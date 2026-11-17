package main

import (
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync/atomic"
	"testing"

	"github.com/maximhq/bifrost/core/schemas"
)

const staleAnswer = "The EU AI Act is still a proposal and has not been adopted by the European Union."

func chatResponse(content string) []byte {
	b, _ := json.Marshal(map[string]any{
		"id": "chatcmpl-1", "object": "chat.completion", "model": "claude-haiku-5-5",
		"choices": []any{map[string]any{"index": 0, "finish_reason": "stop",
			"message": map[string]any{"role": "assistant", "content": content}}},
	})
	return b
}

var chatRequest = []byte(`{"model":"anthropic/claude-haiku-5-5","messages":[{"role":"user","content":"Is the EU AI Act law?"}]}`)

// fakeChecker returns a canned PolicyPulse /check result and counts calls.
func fakeChecker(t *testing.T, score int, stale bool, corrected string) (*httptest.Server, *int32) {
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
		if corrected != "" {
			out["corrected_answer"] = corrected
		}
		_ = json.NewEncoder(w).Encode(out)
	}))
	t.Cleanup(srv.Close)
	return srv, &calls
}

func newTestGuard(t *testing.T, url, mode string) *Guard {
	t.Helper()
	cfg, err := parseConfig(map[string]any{"checker_url": url + "/check", "mode": mode, "min_score": 40, "min_chars": 10})
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
	srv, calls := fakeChecker(t, 10, true, "The EU AI Act was adopted in 2024 and is in force.")
	g := newTestGuard(t, srv.URL, ModeAnnotate)

	out := g.Process(nil, "/v1/chat/completions", chatRequest, 200, chatResponse(staleAnswer))

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
	if out.Headers["x-freshness-score"] != "10" || out.Headers["x-freshness-stale-claims"] != "1" {
		t.Errorf("unexpected headers: %v", out.Headers)
	}
}

func TestFixReplacesStaleAnswer(t *testing.T) {
	fixed := "The EU AI Act was adopted in 2024 and is in force."
	srv, _ := fakeChecker(t, 10, true, fixed)
	g := newTestGuard(t, srv.URL, ModeFix)

	out := g.Process(nil, "/v1/chat/completions", chatRequest, 200, chatResponse(staleAnswer))

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
	srv, _ := fakeChecker(t, 90, false, "")
	g := newTestGuard(t, srv.URL, ModeFix)
	out := g.Process(nil, "/v1/chat/completions", chatRequest, 200, chatResponse(staleAnswer))
	if out.Action != "annotated" || content(t, decode(t, out.Body)) != staleAnswer {
		t.Fatalf("a current answer must not be rewritten (action=%q)", out.Action)
	}
}

func TestBlockReturns422BelowThreshold(t *testing.T) {
	srv, _ := fakeChecker(t, 10, true, "corrected")
	g := newTestGuard(t, srv.URL, ModeBlock)

	out := g.Process(nil, "/v1/chat/completions", chatRequest, 200, chatResponse(staleAnswer))

	if out.Action != "blocked" || out.Status != http.StatusUnprocessableEntity {
		t.Fatalf("action=%q status=%d", out.Action, out.Status)
	}
	e := decode(t, out.Body)["error"].(map[string]any)
	if e["type"] != "stale_answer" || !strings.Contains(e["message"].(string), "10/100") {
		t.Errorf("unexpected error body: %v", e)
	}
	if _, hasChoices := decode(t, out.Body)["choices"]; hasChoices {
		t.Error("a blocked response must not contain the stale answer as a choice")
	}
	if e["freshness"].(map[string]any)["original_answer"] != staleAnswer {
		t.Error("the blocked answer should be kept in freshness.original_answer for audit")
	}
}

func TestBlockPassesFreshAnswer(t *testing.T) {
	srv, _ := fakeChecker(t, 90, false, "")
	g := newTestGuard(t, srv.URL, ModeBlock)
	out := g.Process(nil, "/v1/chat/completions", chatRequest, 200, chatResponse(staleAnswer))
	if out.Status != 0 || out.Action != "annotated" {
		t.Fatalf("a fresh answer must pass in block mode (action=%q status=%d)", out.Action, out.Status)
	}
}

func TestHeaderOverridesMode(t *testing.T) {
	srv, calls := fakeChecker(t, 10, true, "fixed text here")
	g := newTestGuard(t, srv.URL, ModeAnnotate)

	if out := g.Process(map[string]string{"X-Freshness-Guard": "off"}, "/v1/chat/completions", chatRequest, 200,
		chatResponse(staleAnswer)); out.Action != "" || *calls != 0 {
		t.Fatalf("off header must skip the check entirely (action=%q calls=%d)", out.Action, *calls)
	}
	if out := g.Process(map[string]string{"x-freshness-guard": "fix"}, "/v1/chat/completions", chatRequest, 200,
		chatResponse(staleAnswer)); out.Action != "fixed" {
		t.Fatalf("fix header must switch to fix mode, got %q", out.Action)
	}
}

func TestSkipsWhatItShouldNotTouch(t *testing.T) {
	srv, calls := fakeChecker(t, 10, true, "")
	g := newTestGuard(t, srv.URL, ModeAnnotate)
	cases := []struct {
		name, path string
		req        []byte
		status     int
		resp       []byte
		wantAction string
	}{
		{"embeddings route", "/v1/embeddings", chatRequest, 200, chatResponse(staleAnswer), ""},
		{"provider error", "/v1/chat/completions", chatRequest, 500, []byte(`{"error":"x"}`), ""},
		{"streaming", "/v1/chat/completions", []byte(`{"stream":true}`), 200, chatResponse(staleAnswer), "skipped:stream"},
		{"not json", "/v1/chat/completions", chatRequest, 200, []byte("data: chunk"), "skipped:unparsed"},
		{"short answer", "/v1/chat/completions", chatRequest, 200, chatResponse("Yes."), "skipped:short"},
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
	g := newTestGuard(t, down, ModeAnnotate)
	out := g.Process(nil, "/v1/chat/completions", chatRequest, 200, chatResponse(staleAnswer))
	m := decode(t, out.Body)
	if out.Action != "fail-open" || content(t, m) != staleAnswer || m["freshness"].(map[string]any)["status"] != "unchecked" {
		t.Fatalf("fail-open must pass the answer through marked unchecked (action=%q)", out.Action)
	}

	cfg, _ := parseConfig(map[string]any{"checker_url": down + "/check", "fail_open": false, "min_chars": 10})
	out = NewGuard(cfg).Process(nil, "/v1/chat/completions", chatRequest, 200, chatResponse(staleAnswer))
	if out.Action != "fail-closed" || out.Status != http.StatusServiceUnavailable {
		t.Fatalf("fail-closed must return 503 (action=%q status=%d)", out.Action, out.Status)
	}
}

func TestContentPartsAreRead(t *testing.T) {
	srv, _ := fakeChecker(t, 10, true, "")
	g := newTestGuard(t, srv.URL, ModeAnnotate)
	resp, _ := json.Marshal(map[string]any{"choices": []any{map[string]any{"message": map[string]any{
		"role": "assistant", "content": []any{map[string]any{"type": "text", "text": staleAnswer}}}}}})
	if out := g.Process(nil, "/v1/chat/completions", chatRequest, 200, resp); out.Action != "annotated" {
		t.Fatalf("array content parts must be checked, got %q", out.Action)
	}
}

func TestParseConfig(t *testing.T) {
	cfg, err := parseConfig(nil)
	if err != nil || cfg.Mode != ModeAnnotate || !*cfg.FailOpen || cfg.TimeoutMS != 90000 {
		t.Fatalf("defaults wrong: %+v err=%v", cfg, err)
	}
	if _, err := parseConfig(map[string]any{"mode": "shout"}); err == nil {
		t.Error("unknown mode must be rejected")
	}
	if _, err := parseConfig(map[string]any{"checker_url": ""}); err == nil {
		t.Error("empty checker_url must be rejected")
	}
}

// TestHookWithBifrostTypes drives the exported hook with Bifrost's own request/response types.
func TestHookWithBifrostTypes(t *testing.T) {
	srv, _ := fakeChecker(t, 10, true, "The EU AI Act is in force.")
	if err := Init(map[string]any{"checker_url": srv.URL + "/check", "mode": "fix", "min_chars": 10}); err != nil {
		t.Fatal(err)
	}
	if GetName() != "freshness-guard" {
		t.Fatalf("GetName = %q", GetName())
	}
	req := &schemas.HTTPRequest{Method: "POST", Path: "/v1/chat/completions", Headers: map[string]string{}, Body: chatRequest}
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
