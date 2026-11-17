package main

import (
	"context"
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"github.com/maximhq/bifrost/core/schemas"
)

const (
	staleAnswer    = "The EU AI Act is still a proposal and has not been adopted by the European Union."
	timelessAnswer = "Sure! Here is a function that reverses a string: it walks the characters from the end to the start."
	chatPath       = "/v1/chat/completions"
)

func chatResponse(content string) []byte {
	b, _ := json.Marshal(map[string]any{
		"id": "chatcmpl-1", "object": "chat.completion", "model": "claude-haiku-5-5",
		"choices": []any{map[string]any{"index": 0, "finish_reason": "stop",
			"message": map[string]any{"role": "assistant", "content": content}}},
	})
	return b
}

var chatRequest = []byte(`{"model":"anthropic/claude-haiku-5-5","messages":[{"role":"user","content":"Is the EU AI Act law?"}]}`)

// fakeChecker returns a canned check result and counts calls. delay simulates a slow live check.
func fakeChecker(t *testing.T, score int, stale bool, corrected string, delay time.Duration) (*httptest.Server, *int32) {
	t.Helper()
	var calls int32
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		atomic.AddInt32(&calls, 1)
		if r.Method != http.MethodPost || r.URL.Path != "/check" {
			http.Error(w, "bad route", http.StatusNotFound)
			return
		}
		body, _ := io.ReadAll(r.Body)
		var in map[string]any
		if err := json.Unmarshal(body, &in); err != nil || in["answer"] == "" {
			http.Error(w, "bad body", http.StatusBadRequest)
			return
		}
		time.Sleep(delay)
		verdict := "Current"
		if stale {
			verdict = "Outdated"
		}
		out := map[string]any{
			"overall_freshness": score, "has_stale_claims": stale, "checked_at": "2026-11-17T10:00:00",
			"claims": []any{map[string]any{"claim": "The EU AI Act is still a proposal.", "verdict": verdict,
				"freshness_score": score, "suggested_correction": corrected,
				"sources": []any{map[string]any{"url": "https://eur-lex.europa.eu/eli/reg/2024/1689/oj", "official": true}}}},
		}
		if fix, _ := in["fix"].(bool); fix && corrected != "" {
			out["corrected_answer"] = corrected
		}
		_ = json.NewEncoder(w).Encode(out)
	}))
	t.Cleanup(srv.Close)
	return srv, &calls
}

// fakeWebhook collects audit events.
type fakeWebhook struct {
	mu     sync.Mutex
	events []AuditEvent
	srv    *httptest.Server
}

func newWebhook(t *testing.T) *fakeWebhook {
	t.Helper()
	h := &fakeWebhook{}
	h.srv = httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		var ev AuditEvent
		if json.NewDecoder(r.Body).Decode(&ev) == nil {
			h.mu.Lock()
			h.events = append(h.events, ev)
			h.mu.Unlock()
		}
	}))
	t.Cleanup(h.srv.Close)
	return h
}

func (h *fakeWebhook) all() []AuditEvent {
	h.mu.Lock()
	defer h.mu.Unlock()
	return append([]AuditEvent(nil), h.events...)
}

func newTestGuard(t *testing.T, url, mode string, extra map[string]any) *Guard {
	t.Helper()
	raw := map[string]any{"checker_url": url + "/check", "mode": mode, "min_score": 40, "min_chars": 10}
	for k, v := range extra {
		raw[k] = v
	}
	cfg, err := parseConfig(raw)
	if err != nil {
		t.Fatalf("parseConfig: %v", err)
	}
	return NewGuard(cfg)
}

func decode(t *testing.T, b []byte) map[string]any {
	t.Helper()
	var m map[string]any
	if err := json.Unmarshal(b, &m); err != nil {
		t.Fatalf("response is not JSON: %v\n%s", err, b)
	}
	return m
}

func content(t *testing.T, m map[string]any) string {
	t.Helper()
	return m["choices"].([]any)[0].(map[string]any)["message"].(map[string]any)["content"].(string)
}

func TestAnnotateAddsFreshnessAndHeaders(t *testing.T) {
	srv, calls := fakeChecker(t, 10, true, "The EU AI Act was adopted in 2024 and is in force.", 0)
	g := newTestGuard(t, srv.URL, ModeAnnotate, nil)

	out := g.Process(nil, chatPath, chatRequest, 200, chatResponse(staleAnswer))

	if out.Action != "annotated" || *calls != 1 {
		t.Fatalf("action=%q calls=%d", out.Action, *calls)
	}
	m := decode(t, out.Body)
	if got := content(t, m); got != staleAnswer {
		t.Errorf("annotate must not change the answer, got %q", got)
	}
	f := m["freshness"].(map[string]any)
	if f["status"] != "checked" || f["has_stale_claims"] != true || f["overall_score"].(float64) != 10 {
		t.Errorf("unexpected freshness object: %v", f)
	}
	if out.Headers["x-freshness-score"] != "10" || out.Headers["x-freshness-stale-claims"] != "1" ||
		out.Headers["x-freshness-cache"] != "miss" || out.Headers["x-freshness-check-id"] == "" {
		t.Errorf("unexpected headers: %v", out.Headers)
	}
}

func TestFixReplacesStaleAnswer(t *testing.T) {
	fixed := "The EU AI Act was adopted in 2024 and is in force."
	srv, _ := fakeChecker(t, 10, true, fixed, 0)
	g := newTestGuard(t, srv.URL, ModeFix, nil)

	out := g.Process(nil, chatPath, chatRequest, 200, chatResponse(staleAnswer))

	m := decode(t, out.Body)
	if out.Action != "fixed" || content(t, m) != fixed {
		t.Fatalf("action=%q content=%q", out.Action, content(t, m))
	}
	if m["freshness"].(map[string]any)["original_answer"] != staleAnswer {
		t.Error("fix mode must keep the original answer in the freshness object")
	}
	if out.Headers["x-freshness-status"] != "fixed" {
		t.Errorf("status header = %q", out.Headers["x-freshness-status"])
	}
}

func TestFixLeavesCurrentAnswerAlone(t *testing.T) {
	srv, _ := fakeChecker(t, 90, false, "", 0)
	g := newTestGuard(t, srv.URL, ModeFix, nil)
	out := g.Process(nil, chatPath, chatRequest, 200, chatResponse(staleAnswer))
	if out.Action != "annotated" || content(t, decode(t, out.Body)) != staleAnswer {
		t.Fatalf("a current answer must not be rewritten (action=%q)", out.Action)
	}
}

func TestBlockReturns422BelowThreshold(t *testing.T) {
	srv, _ := fakeChecker(t, 10, true, "corrected", 0)
	g := newTestGuard(t, srv.URL, ModeBlock, nil)

	out := g.Process(nil, chatPath, chatRequest, 200, chatResponse(staleAnswer))

	if out.Action != "blocked" || out.Status != http.StatusUnprocessableEntity {
		t.Fatalf("action=%q status=%d", out.Action, out.Status)
	}
	body := decode(t, out.Body)
	e := body["error"].(map[string]any)
	if e["type"] != "stale_answer" || !strings.Contains(e["message"].(string), "10/100") {
		t.Errorf("unexpected error body: %v", e)
	}
	if _, hasChoices := body["choices"]; hasChoices {
		t.Error("a blocked response must not contain the stale answer as a choice")
	}
	if e["freshness"].(map[string]any)["original_answer"] != staleAnswer {
		t.Error("the blocked answer should be kept in freshness.original_answer for audit")
	}
}

func TestBlockPassesFreshAnswer(t *testing.T) {
	srv, _ := fakeChecker(t, 90, false, "", 0)
	g := newTestGuard(t, srv.URL, ModeBlock, nil)
	out := g.Process(nil, chatPath, chatRequest, 200, chatResponse(staleAnswer))
	if out.Status != 0 || out.Action != "annotated" {
		t.Fatalf("a fresh answer must pass in block mode (action=%q status=%d)", out.Action, out.Status)
	}
}

func TestHeaderOverridesMode(t *testing.T) {
	srv, calls := fakeChecker(t, 10, true, "fixed text here", 0)
	g := newTestGuard(t, srv.URL, ModeAnnotate, nil)

	if out := g.Process(map[string]string{"X-Freshness-Guard": "off"}, chatPath, chatRequest, 200,
		chatResponse(staleAnswer)); out.Action != "" || *calls != 0 {
		t.Fatalf("off header must skip the check entirely (action=%q calls=%d)", out.Action, *calls)
	}
	if out := g.Process(map[string]string{"x-freshness-guard": "fix"}, chatPath, chatRequest, 200,
		chatResponse(staleAnswer)); out.Action != "fixed" {
		t.Fatalf("fix header must switch to fix mode, got %q", out.Action)
	}
}

func TestAuditModeReturnsAtOnceAndReports(t *testing.T) {
	srv, _ := fakeChecker(t, 10, true, "", 300*time.Millisecond)
	hook := newWebhook(t)
	g := newTestGuard(t, srv.URL, ModeAudit, map[string]any{"audit_webhook_url": hook.srv.URL})

	t0 := time.Now()
	out := g.Process(nil, chatPath, chatRequest, 200, chatResponse(staleAnswer))
	took := time.Since(t0)

	if out.Action != "audit-pending" || out.Body != nil || out.Status != 0 {
		t.Fatalf("audit mode must leave the response untouched (action=%q)", out.Action)
	}
	if took > 150*time.Millisecond {
		t.Errorf("audit mode must not wait for the checker, took %v", took)
	}
	id := out.Headers["x-freshness-check-id"]
	if out.Headers["x-freshness-status"] != "pending" || id == "" {
		t.Errorf("headers: %v", out.Headers)
	}
	g.Wait(5 * time.Second)
	evs := hook.all()
	if len(evs) != 1 {
		t.Fatalf("want 1 audit event, got %d", len(evs))
	}
	ev := evs[0]
	if ev.CheckID != id || ev.Action != "audited" || ev.Score == nil || *ev.Score != 10 || ev.StaleClaims != 1 ||
		ev.Model != "anthropic/claude-haiku-5-5" || ev.Source != sourceName || ev.CheckMS < 250 {
		t.Errorf("unexpected audit event: %+v", ev)
	}
}

func TestSyncModesAlsoReportToWebhook(t *testing.T) {
	srv, _ := fakeChecker(t, 10, true, "", 0)
	hook := newWebhook(t)
	g := newTestGuard(t, srv.URL, ModeBlock, map[string]any{"audit_webhook_url": hook.srv.URL})
	g.Process(nil, chatPath, chatRequest, 200, chatResponse(staleAnswer))
	g.Wait(5 * time.Second)
	if evs := hook.all(); len(evs) != 1 || evs[0].Action != "blocked" || evs[0].Mode != ModeBlock {
		t.Fatalf("unexpected events: %+v", evs)
	}
}

func TestCacheServesRepeatAnswers(t *testing.T) {
	srv, calls := fakeChecker(t, 10, true, "The EU AI Act is in force.", 0)
	g := newTestGuard(t, srv.URL, ModeAnnotate, nil)

	g.Process(nil, chatPath, chatRequest, 200, chatResponse(staleAnswer))
	out := g.Process(nil, chatPath, chatRequest, 200, chatResponse(staleAnswer))
	if *calls != 1 || out.Headers["x-freshness-cache"] != "hit" || out.Action != "annotated" {
		t.Fatalf("second identical answer must come from the cache (calls=%d headers=%v)", *calls, out.Headers)
	}
	// fix mode needs the corrected text, which the annotate check did not fetch
	fix := map[string]string{headerControl: "fix"}
	if out = g.Process(fix, chatPath, chatRequest, 200, chatResponse(staleAnswer)); *calls != 2 || out.Action != "fixed" {
		t.Fatalf("fix after annotate must re-check once (calls=%d action=%q)", *calls, out.Action)
	}
	if out = g.Process(fix, chatPath, chatRequest, 200, chatResponse(staleAnswer)); *calls != 2 || out.Action != "fixed" ||
		out.Headers["x-freshness-cache"] != "hit" {
		t.Fatalf("repeat fix must be cached (calls=%d headers=%v)", *calls, out.Headers)
	}
	// audit mode serves known answers at once, annotated
	out = g.Process(map[string]string{headerControl: "audit"}, chatPath, chatRequest, 200, chatResponse(staleAnswer))
	if *calls != 2 || out.Action != "annotated" || out.Headers["x-freshness-status"] != "checked" {
		t.Fatalf("audit mode must use the cache (calls=%d action=%q)", *calls, out.Action)
	}
}

func TestCacheExpiresAndCanBeDisabled(t *testing.T) {
	srv, calls := fakeChecker(t, 10, true, "", 0)
	g := newTestGuard(t, srv.URL, ModeAnnotate, map[string]any{"cache_ttl_seconds": 60})
	now := time.Now()
	g.now = func() time.Time { return now }
	g.Process(nil, chatPath, chatRequest, 200, chatResponse(staleAnswer))
	now = now.Add(2 * time.Minute)
	g.Process(nil, chatPath, chatRequest, 200, chatResponse(staleAnswer))
	if *calls != 2 {
		t.Fatalf("expired entries must be re-checked, calls=%d", *calls)
	}
	off := newTestGuard(t, srv.URL, ModeAnnotate, map[string]any{"cache_ttl_seconds": 0})
	off.Process(nil, chatPath, chatRequest, 200, chatResponse(staleAnswer))
	off.Process(nil, chatPath, chatRequest, 200, chatResponse(staleAnswer))
	if *calls != 4 {
		t.Fatalf("cache_ttl_seconds 0 must disable the cache, calls=%d", *calls)
	}
}

func TestTimelessAnswersAreSkipped(t *testing.T) {
	srv, calls := fakeChecker(t, 10, true, "", 0)
	g := newTestGuard(t, srv.URL, ModeAnnotate, nil)
	out := g.Process(nil, chatPath, chatRequest, 200, chatResponse(timelessAnswer))
	if out.Action != "skipped:timeless" || out.Body != nil || *calls != 0 {
		t.Fatalf("answers with nothing time-sensitive must be skipped (action=%q calls=%d)", out.Action, *calls)
	}
	all := newTestGuard(t, srv.URL, ModeAnnotate, map[string]any{"only_time_sensitive": false})
	if out = all.Process(nil, chatPath, chatRequest, 200, chatResponse(timelessAnswer)); out.Action != "annotated" {
		t.Fatalf("only_time_sensitive=false must check everything, got %q", out.Action)
	}
	for _, s := range []string{"The fee is $20 per month.", "Version v2.5.1 was released.", "The deadline is in 2027.",
		"Their CEO announced it.", "VAT is 12% there."} {
		if !looksTimeSensitive(s) {
			t.Errorf("should look time-sensitive: %q", s)
		}
	}
}

func TestSamplingAndExplicitHeader(t *testing.T) {
	srv, calls := fakeChecker(t, 10, true, "", 0)
	g := newTestGuard(t, srv.URL, ModeAnnotate, map[string]any{"sample_rate": 0.25})
	g.rand = func() float64 { return 0.9 } // above the rate: not sampled
	if out := g.Process(nil, chatPath, chatRequest, 200, chatResponse(staleAnswer)); out.Action != "skipped:sampled" || *calls != 0 {
		t.Fatalf("unsampled answers must be skipped (action=%q)", out.Action)
	}
	if out := g.Process(map[string]string{headerControl: "annotate"}, chatPath, chatRequest, 200,
		chatResponse(staleAnswer)); out.Action != "annotated" {
		t.Fatalf("an explicit header must bypass sampling, got %q", out.Action)
	}
	g.rand = func() float64 { return 0.1 }
	if out := g.Process(nil, chatPath, chatRequest, 200, chatResponse(staleAnswer+" Again in 2026.")); out.Action != "annotated" {
		t.Fatalf("sampled answers must be checked, got %q", out.Action)
	}
}

func TestModelFilters(t *testing.T) {
	srv, _ := fakeChecker(t, 10, true, "", 0)
	skip := newTestGuard(t, srv.URL, ModeAnnotate, map[string]any{"skip_models": []string{"anthropic/*"}})
	if out := skip.Process(nil, chatPath, chatRequest, 200, chatResponse(staleAnswer)); out.Action != "skipped:model" {
		t.Fatalf("skip_models must skip matching models, got %q", out.Action)
	}
	only := newTestGuard(t, srv.URL, ModeAnnotate, map[string]any{"only_models": []string{"groq/"}})
	if out := only.Process(nil, chatPath, chatRequest, 200, chatResponse(staleAnswer)); out.Action != "skipped:model" {
		t.Fatalf("only_models must skip other models, got %q", out.Action)
	}
	groq := []byte(`{"model":"groq/openai/gpt-oss-120b"}`)
	if out := only.Process(nil, chatPath, groq, 200, chatResponse(staleAnswer)); out.Action != "annotated" {
		t.Fatalf("only_models must check matching models, got %q", out.Action)
	}
}

func TestBackgroundChecksAreBounded(t *testing.T) {
	srv, _ := fakeChecker(t, 10, true, "", 300*time.Millisecond)
	g := newTestGuard(t, srv.URL, ModeAudit, map[string]any{"max_concurrent": 1})
	first := g.Process(nil, chatPath, chatRequest, 200, chatResponse(staleAnswer))
	second := g.Process(nil, chatPath, chatRequest, 200, chatResponse(staleAnswer+" It was due in 2025."))
	if first.Action != "audit-pending" || second.Action != "skipped:busy" {
		t.Fatalf("want pending then busy, got %q then %q", first.Action, second.Action)
	}
	g.Wait(5 * time.Second)
}

func TestSkipsWhatItShouldNotTouch(t *testing.T) {
	srv, calls := fakeChecker(t, 10, true, "", 0)
	g := newTestGuard(t, srv.URL, ModeAnnotate, nil)
	cases := []struct {
		name, path string
		req        []byte
		status     int
		resp       []byte
		wantAction string
	}{
		{"embeddings route", "/v1/embeddings", chatRequest, 200, chatResponse(staleAnswer), ""},
		{"provider error", chatPath, chatRequest, 500, []byte(`{"error":"x"}`), ""},
		{"streaming request", chatPath, []byte(`{"stream":true}`), 200, chatResponse(staleAnswer), ""},
		{"not json", chatPath, chatRequest, 200, []byte("data: chunk"), "skipped:unparsed"},
		{"short answer", chatPath, chatRequest, 200, chatResponse("Yes."), "skipped:short"},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			out := g.Process(nil, tc.path, tc.req, tc.status, tc.resp)
			if out.Action != tc.wantAction || out.Body != nil {
				t.Errorf("action=%q body changed=%v", out.Action, out.Body != nil)
			}
		})
	}
	if *calls != 0 {
		t.Errorf("checker should not be called for skipped responses, got %d calls", *calls)
	}
}

func TestFailOpenAndFailClosed(t *testing.T) {
	down := "http://127.0.0.1:1" // nothing listens here
	g := newTestGuard(t, down, ModeAnnotate, nil)
	out := g.Process(nil, chatPath, chatRequest, 200, chatResponse(staleAnswer))
	m := decode(t, out.Body)
	if out.Action != "fail-open" || content(t, m) != staleAnswer || m["freshness"].(map[string]any)["status"] != "unchecked" {
		t.Fatalf("fail-open must pass the answer through marked unchecked (action=%q)", out.Action)
	}
	closed := newTestGuard(t, down, ModeAnnotate, map[string]any{"fail_open": false})
	out = closed.Process(nil, chatPath, chatRequest, 200, chatResponse(staleAnswer))
	if out.Action != "fail-closed" || out.Status != http.StatusServiceUnavailable {
		t.Fatalf("fail-closed must return 503 (action=%q status=%d)", out.Action, out.Status)
	}
}

func TestContentPartsAreRead(t *testing.T) {
	srv, _ := fakeChecker(t, 10, true, "", 0)
	g := newTestGuard(t, srv.URL, ModeAnnotate, nil)
	resp, _ := json.Marshal(map[string]any{"choices": []any{map[string]any{"message": map[string]any{
		"role": "assistant", "content": []any{map[string]any{"type": "text", "text": staleAnswer}}}}}})
	if out := g.Process(nil, chatPath, chatRequest, 200, resp); out.Action != "annotated" {
		t.Fatalf("array content parts must be checked, got %q", out.Action)
	}
}

func TestParseConfig(t *testing.T) {
	cfg, err := parseConfig(nil)
	if err != nil || cfg.Mode != ModeAnnotate || !*cfg.FailOpen || cfg.TimeoutMS != 90000 || !*cfg.OnlyTimeSensitive ||
		*cfg.SampleRate != 1 || *cfg.CacheTTLSeconds != 21600 || cfg.MaxConcurrent != 4 {
		t.Fatalf("defaults wrong: %+v err=%v", cfg, err)
	}
	for name, bad := range map[string]map[string]any{
		"unknown mode":      {"mode": "shout"},
		"empty checker_url": {"checker_url": ""},
		"sample_rate > 1":   {"sample_rate": 1.5},
	} {
		if _, err := parseConfig(bad); err == nil {
			t.Errorf("%s must be rejected", name)
		}
	}
	if cfg, err := parseConfig(map[string]any{"mode": "Audit"}); err != nil || cfg.Mode != ModeAudit {
		t.Errorf("audit mode must be accepted: %v", err)
	}
}

// TestHookWithBifrostTypes drives the exported post-hook with Bifrost's own request/response types.
func TestHookWithBifrostTypes(t *testing.T) {
	srv, _ := fakeChecker(t, 10, true, "The EU AI Act is in force.", 0)
	if err := Init(map[string]any{"checker_url": srv.URL + "/check", "mode": "fix", "min_chars": 10}); err != nil {
		t.Fatal(err)
	}
	if GetName() != "freshness-guard" {
		t.Fatalf("GetName = %q", GetName())
	}
	req := &schemas.HTTPRequest{Method: "POST", Path: chatPath, Headers: map[string]string{}, Body: chatRequest}
	resp := &schemas.HTTPResponse{StatusCode: 200, Headers: map[string]string{"Content-Length": "999"}, Body: chatResponse(staleAnswer)}

	if err := HTTPTransportPostHook(nil, req, resp); err != nil {
		t.Fatal(err)
	}
	if resp.Headers["x-freshness-status"] != "fixed" {
		t.Errorf("headers: %v", resp.Headers)
	}
	if _, ok := resp.Headers["Content-Length"]; ok {
		t.Error("stale Content-Length must be removed after the body changes")
	}
	if got := content(t, decode(t, resp.Body)); got != "The EU AI Act is in force." {
		t.Errorf("content = %q", got)
	}
	if err := Cleanup(); err != nil {
		t.Fatal(err)
	}
}

func streamChunk(text string, finish string) *schemas.BifrostStreamChunk {
	choice := schemas.BifrostResponseChoice{
		ChatStreamResponseChoice: &schemas.ChatStreamResponseChoice{
			Delta: &schemas.ChatStreamResponseChoiceDelta{Content: &text}},
	}
	if finish != "" {
		choice.FinishReason = &finish
	}
	return &schemas.BifrostStreamChunk{BifrostChatResponse: &schemas.BifrostChatResponse{
		Choices: []schemas.BifrostResponseChoice{choice}}}
}

// streamCtx mirrors Bifrost: every chunk gets a fresh context that carries the same request ID.
func streamCtx(t *testing.T, id string) *schemas.BifrostContext {
	t.Helper()
	ctx, cancel := schemas.NewBifrostContextWithTimeout(context.Background(), time.Minute)
	t.Cleanup(cancel)
	ctx.SetValue(schemas.BifrostContextKeyRequestID, id)
	return ctx
}

// TestStreamIsCollectedAndAudited drives the stream hooks with Bifrost's context and chunk types.
func TestStreamIsCollectedAndAudited(t *testing.T) {
	srv, calls := fakeChecker(t, 10, true, "", 0)
	hook := newWebhook(t)
	if err := Init(map[string]any{"checker_url": srv.URL + "/check", "mode": "block", "min_chars": 10,
		"audit_webhook_url": hook.srv.URL}); err != nil {
		t.Fatal(err)
	}
	const id = "req-stream-1"
	req := &schemas.HTTPRequest{Method: "POST", Path: chatPath, Headers: map[string]string{},
		Body: []byte(`{"model":"groq/openai/gpt-oss-120b","stream":true}`)}

	meta := &schemas.HTTPResponseMetadata{StatusCode: 200, Headers: map[string]string{"X-Request-Id": id}}
	if err := HTTPTransportResponseHeadersHook(nil, req, meta); err != nil {
		t.Fatal(err)
	}
	if meta.Header("x-freshness-status") != "pending" || meta.Header("x-freshness-check-id") != id {
		t.Fatalf("stream headers: %v", meta.Headers)
	}

	// Bifrost does not pass the request body to the chunk hook, and gives each chunk its own context.
	noBody := &schemas.HTTPRequest{Method: "POST", Path: chatPath, Headers: map[string]string{}}
	parts := []string{"The EU AI Act is still a proposal ", "and has not been adopted ", "by the European Union."}
	for i, p := range parts {
		finish := ""
		if i == len(parts)-1 {
			finish = "stop"
		}
		in := streamChunk(p, finish)
		in.BifrostChatResponse.Model = "openai/gpt-oss-120b"
		in.BifrostChatResponse.ExtraFields.Provider = "groq"
		out, err := HTTPTransportStreamChunkHook(streamCtx(t, id), noBody, in)
		if err != nil || out != in {
			t.Fatalf("chunks must pass through untouched (err=%v)", err)
		}
	}
	// a trailing chunk after the finish (for example usage) must not be checked on its own
	if _, err := HTTPTransportStreamChunkHook(streamCtx(t, id), noBody, streamChunk("", "")); err != nil {
		t.Fatal(err)
	}
	streams.drop(id)
	current().Wait(5 * time.Second)

	evs := hook.all()
	if *calls != 1 || len(evs) != 1 {
		t.Fatalf("want one check and one event, got calls=%d events=%d", *calls, len(evs))
	}
	ev := evs[0]
	if !ev.Stream || ev.Action != "audited" || ev.CheckID != id || ev.Model != "groq/openai/gpt-oss-120b" ||
		ev.AnswerExcerpt != staleAnswer {
		t.Errorf("unexpected stream audit event: %+v", ev)
	}

	// two streams at once do not mix their text
	a, b := streamCtx(t, "req-a"), streamCtx(t, "req-b")
	_, _ = HTTPTransportStreamChunkHook(a, noBody, streamChunk("The price was $5 in 2023, ", ""))
	_, _ = HTTPTransportStreamChunkHook(b, noBody, streamChunk("The law was adopted in 2019, ", ""))
	_, _ = HTTPTransportStreamChunkHook(a, noBody, streamChunk("and it is still the current price.", "stop"))
	_, _ = HTTPTransportStreamChunkHook(b, noBody, streamChunk("and it is still in force today.", "stop"))
	current().Wait(5 * time.Second)
	got := map[string]string{}
	for _, e := range hook.all() {
		got[e.CheckID] = e.AnswerExcerpt
	}
	if got["req-a"] != "The price was $5 in 2023, and it is still the current price." ||
		got["req-b"] != "The law was adopted in 2019, and it is still in force today." {
		t.Errorf("streams were mixed: %v", got)
	}

	// mode off and non-streaming requests are left alone
	off := &schemas.HTTPRequest{Method: "POST", Path: chatPath, Headers: map[string]string{headerControl: "off"}}
	before := *calls
	_, _ = HTTPTransportStreamChunkHook(streamCtx(t, "req-off"), off, streamChunk(staleAnswer, "stop"))
	current().Wait(time.Second)
	if *calls != before {
		t.Errorf("mode off must not check streams, calls went from %d to %d", before, *calls)
	}
	plain := &schemas.HTTPRequest{Method: "POST", Path: chatPath, Headers: map[string]string{}, Body: chatRequest}
	meta = &schemas.HTTPResponseMetadata{StatusCode: 200, Headers: map[string]string{}}
	_ = HTTPTransportResponseHeadersHook(nil, plain, meta)
	if meta.Header("x-freshness-status") != "" {
		t.Error("the headers hook must only mark streaming responses")
	}
}

// TestCheckIDIsBifrostRequestID: audit events carry Bifrost's request ID so they can be joined with its logs.
func TestCheckIDIsBifrostRequestID(t *testing.T) {
	srv, _ := fakeChecker(t, 10, true, "", 0)
	if err := Init(map[string]any{"checker_url": srv.URL + "/check", "mode": "annotate", "min_chars": 10}); err != nil {
		t.Fatal(err)
	}
	req := &schemas.HTTPRequest{Method: "POST", Path: chatPath, Headers: map[string]string{}, Body: chatRequest}
	resp := &schemas.HTTPResponse{StatusCode: 200, Headers: map[string]string{}, Body: chatResponse(staleAnswer)}
	if err := HTTPTransportPostHook(streamCtx(t, "req-42"), req, resp); err != nil {
		t.Fatal(err)
	}
	if resp.Headers["x-freshness-check-id"] != "req-42" {
		t.Errorf("check id = %q, want the Bifrost request ID", resp.Headers["x-freshness-check-id"])
	}
}
