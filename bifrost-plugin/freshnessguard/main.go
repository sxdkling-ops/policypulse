// Freshness Guard: a Bifrost plugin that checks chat completion answers for stale claims
// (regulations, prices, API versions, company facts) before they reach the caller.
//
// It runs as an HTTP transport post-hook. For every non-streaming /chat/completions response it
// sends the answer to a PolicyPulse checker (POST /check), then annotates, fixes or blocks the
// response depending on the configured mode. Build with -buildmode=plugin against the same
// Bifrost core version as the gateway (see the Dockerfile in docker/).
package main

import (
	"strings"
	"sync"

	"github.com/maximhq/bifrost/core/schemas"
)

var (
	mu    sync.RWMutex
	guard *Guard
)

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

// HTTPTransportPostHook runs after Bifrost has the provider's response and before it is sent to the client.
func HTTPTransportPostHook(ctx *schemas.BifrostContext, req *schemas.HTTPRequest, resp *schemas.HTTPResponse) error {
	mu.RLock()
	g := guard
	mu.RUnlock()
	if g == nil || req == nil || resp == nil {
		return nil
	}
	out := g.Process(req.Headers, req.Path, req.Body, resp.StatusCode, resp.Body)
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
		if resp.Headers != nil {
			for k := range resp.Headers {
				if strings.EqualFold(k, "content-length") {
					delete(resp.Headers, k)
				}
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

// Cleanup is called when Bifrost shuts down.
func Cleanup() error {
	return nil
}
