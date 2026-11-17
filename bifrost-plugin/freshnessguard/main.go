// Freshness Guard: a Bifrost plugin that checks chat completion answers for stale claims
// (regulations, prices, API versions, company facts) before or after they reach the caller.
//
// Non-streaming answers go through HTTPTransportPostHook: the answer is sent to a freshness checker
// (POST {answer, fix}) and the response is annotated, fixed or blocked, or (audit mode) returned at once
// and checked in the background. Streamed answers are marked in HTTPTransportResponseHeadersHook, collected
// in HTTPTransportStreamChunkHook and audited once the stream ends. Build with -buildmode=plugin against
// the same Bifrost core version as the gateway (see docker/bifrost.Dockerfile).
package main

import (
	"strings"
	"sync"
	"time"

	"github.com/maximhq/bifrost/core/schemas"
)

const bifrostRequestIDHeader = "x-bifrost-request-id"

var (
	mu    sync.RWMutex
	guard *Guard
)

func current() *Guard {
	mu.RLock()
	defer mu.RUnlock()
	return guard
}

// Init is called once when Bifrost loads the plugin, with the "config" object from config.json.
func Init(config any) error {
	cfg, err := parseConfig(config)
	if err != nil {
		return err
	}
	mu.Lock()
	guard = NewGuard(cfg)
	mu.Unlock()
	return nil
}

// GetName returns the plugin's system identifier.
func GetName() string {
	return "freshness-guard"
}

// requestID returns Bifrost's request ID, so audit events can be matched to Bifrost's own logs.
func requestID(ctx *schemas.BifrostContext) string {
	if ctx == nil {
		return ""
	}
	id, _ := ctx.Value(schemas.BifrostContextKeyRequestID).(string)
	return id
}

// HTTPTransportPostHook runs after Bifrost has the provider's response and before it is sent to the client.
func HTTPTransportPostHook(ctx *schemas.BifrostContext, req *schemas.HTTPRequest, resp *schemas.HTTPResponse) error {
	g := current()
	if g == nil || req == nil || resp == nil {
		return nil
	}
	id := requestID(ctx)
	if id == "" {
		id = headerValue(resp.Headers, bifrostRequestIDHeader)
	}
	out := g.ProcessID(id, req.Headers, req.Path, req.Body, resp.StatusCode, resp.Body)
	if out.Action == "" {
		return nil
	}
	applyOutcome(resp, out)
	if ctx != nil {
		ctx.Log(schemas.LogLevelInfo, "freshness-guard: "+out.Action+" "+req.Path)
	}
	return nil
}

func applyOutcome(resp *schemas.HTTPResponse, out Outcome) {
	if out.Body != nil {
		resp.Body = out.Body
		for k := range resp.Headers {
			if strings.EqualFold(k, "content-length") {
				delete(resp.Headers, k)
			}
		}
	}
	if out.Status != 0 {
		resp.StatusCode = out.Status
	}
	if len(out.Headers) > 0 && resp.Headers == nil {
		resp.Headers = map[string]string{}
	}
	for k, v := range out.Headers {
		resp.Headers[k] = v
	}
}

// HTTPTransportResponseHeadersHook marks streamed chat completions that will be audited once the stream ends.
// For streams the check ID is Bifrost's request ID.
func HTTPTransportResponseHeadersHook(ctx *schemas.BifrostContext, req *schemas.HTTPRequest, resp *schemas.HTTPResponseMetadata) error {
	g := current()
	if g == nil || req == nil || resp == nil || resp.StatusCode != 200 {
		return nil
	}
	if !g.StreamPlan(req.Headers, req.Path, req.Body) {
		return nil
	}
	resp.SetHeader("x-freshness-status", "pending")
	id := requestID(ctx)
	if id == "" {
		id = resp.Header(bifrostRequestIDHeader) // not x-request-id: providers overwrite that one
	}
	if id != "" {
		resp.SetHeader("x-freshness-check-id", id)
	}
	return nil
}

// streamState collects one streamed answer. Bifrost hands every chunk a fresh context and no request body,
// so the state lives here, keyed by Bifrost's request ID.
type streamState struct {
	mode     string
	explicit bool
	model    string
	sb       strings.Builder
	started  time.Time
}

type streamTable struct {
	mu sync.Mutex
	m  map[string]*streamState
}

var streams = &streamTable{m: map[string]*streamState{}}

const (
	streamMaxAge   = 15 * time.Minute // abandoned streams (client went away) are dropped after this
	streamPurgeMin = 256
)

func (t *streamTable) get(id string, create func() *streamState) *streamState {
	t.mu.Lock()
	defer t.mu.Unlock()
	if st, ok := t.m[id]; ok {
		return st
	}
	st := create()
	if st == nil {
		return nil
	}
	if len(t.m) >= streamPurgeMin {
		for k, v := range t.m {
			if time.Since(v.started) > streamMaxAge {
				delete(t.m, k)
			}
		}
	}
	t.m[id] = st
	return st
}

func (t *streamTable) drop(id string) {
	t.mu.Lock()
	delete(t.m, id)
	t.mu.Unlock()
}

// chunkInfo returns the text carried by a chat stream chunk, the model that produced it, and whether it
// ends the answer.
func chunkInfo(chunk *schemas.BifrostStreamChunk) (text, model string, finished bool) {
	if chunk == nil || chunk.BifrostChatResponse == nil {
		return "", "", false
	}
	r := chunk.BifrostChatResponse
	model = r.Model
	if p := string(r.ExtraFields.Provider); p != "" && model != "" && !strings.HasPrefix(model, p+"/") {
		model = p + "/" + model
	}
	if len(r.Choices) == 0 {
		return "", model, false
	}
	c := r.Choices[0]
	if c.ChatStreamResponseChoice != nil && c.ChatStreamResponseChoice.Delta != nil &&
		c.ChatStreamResponseChoice.Delta.Content != nil {
		text = *c.ChatStreamResponseChoice.Delta.Content
	}
	return text, model, c.FinishReason != nil && *c.FinishReason != ""
}

// HTTPTransportStreamChunkHook passes every chunk through untouched while collecting the answer text.
// When the stream ends, the full answer is audited in the background.
func HTTPTransportStreamChunkHook(ctx *schemas.BifrostContext, req *schemas.HTTPRequest, chunk *schemas.BifrostStreamChunk) (*schemas.BifrostStreamChunk, error) {
	g := current()
	id := requestID(ctx)
	if g == nil || req == nil || chunk == nil || id == "" || chunk.BifrostChatResponse == nil {
		return chunk, nil
	}
	st := streams.get(id, func() *streamState {
		if !isChatPath(req.Path) {
			return nil
		}
		mode, explicit := g.resolveMode(req.Headers)
		if mode == ModeOff {
			return nil
		}
		return &streamState{mode: mode, explicit: explicit, started: time.Now()}
	})
	if st == nil {
		return chunk, nil
	}
	text, model, finished := chunkInfo(chunk)
	st.sb.WriteString(text)
	if st.model == "" {
		st.model = model
	}
	if finished {
		streams.drop(id)
		status := g.AuditStream(st.mode, st.explicit, st.model, st.sb.String(), id)
		ctx.Log(schemas.LogLevelInfo, "freshness-guard: stream "+status)
	}
	return chunk, nil
}

// Cleanup is called when Bifrost shuts down. It gives background checks a moment to report.
func Cleanup() error {
	if g := current(); g != nil {
		g.Wait(5 * time.Second)
	}
	return nil
}
