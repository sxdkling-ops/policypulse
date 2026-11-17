package main

import (
	"bytes"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"strconv"
	"strings"
	"time"
)

// Modes the guard can run in. A request can override the configured mode with the
// x-freshness-guard header (off, annotate, fix, block).
const (
	ModeOff      = "off"
	ModeAnnotate = "annotate"
	ModeFix      = "fix"
	ModeBlock    = "block"

	headerControl = "x-freshness-guard"
)

// Config is read from the plugin's "config" object in Bifrost's config.json.
type Config struct {
	CheckerURL string `json:"checker_url"` // PolicyPulse /check endpoint
	Mode       string `json:"mode"`        // default mode: annotate | fix | block | off
	MinScore   int    `json:"min_score"`   // block mode: block when overall freshness < min_score
	TimeoutMS  int    `json:"timeout_ms"`  // how long to wait for the checker
	FailOpen   *bool  `json:"fail_open"`   // checker down: pass the answer through (true) or return 503 (false)
	MinChars   int    `json:"min_chars"`   // skip very short answers
}

func defaultConfig() Config {
	t := true
	return Config{
		CheckerURL: "http://localhost:8000/check",
		Mode:       ModeAnnotate,
		MinScore:   40,
		TimeoutMS:  90000,
		FailOpen:   &t,
		MinChars:   40,
	}
}

// parseConfig accepts whatever Bifrost passes to Init (usually map[string]any) and fills defaults.
func parseConfig(raw any) (Config, error) {
	cfg := defaultConfig()
	if raw == nil {
		return cfg, nil
	}
	b, err := json.Marshal(raw)
	if err != nil {
		return cfg, fmt.Errorf("freshness-guard: config is not JSON-serialisable: %w", err)
	}
	if err := json.Unmarshal(b, &cfg); err != nil {
		return cfg, fmt.Errorf("freshness-guard: invalid config: %w", err)
	}
	if cfg.FailOpen == nil {
		t := true
		cfg.FailOpen = &t
	}
	if cfg.TimeoutMS <= 0 {
		cfg.TimeoutMS = 90000
	}
	cfg.Mode = strings.ToLower(strings.TrimSpace(cfg.Mode))
	if !validMode(cfg.Mode) {
		return cfg, fmt.Errorf("freshness-guard: unknown mode %q (use annotate, fix, block or off)", cfg.Mode)
	}
	if cfg.CheckerURL == "" {
		return cfg, fmt.Errorf("freshness-guard: checker_url is required")
	}
	return cfg, nil
}

func validMode(m string) bool {
	switch m {
	case ModeOff, ModeAnnotate, ModeFix, ModeBlock:
		return true
	}
	return false
}

// CheckResult mirrors the JSON returned by PolicyPulse's POST /check.
type CheckResult struct {
	OverallFreshness *int    `json:"overall_freshness"`
	HasStaleClaims   bool    `json:"has_stale_claims"`
	CorrectedAnswer  *string `json:"corrected_answer"`
	Claims           []Claim `json:"claims"`
	CheckedAt        string  `json:"checked_at"`
}

type Claim struct {
	Claim               string   `json:"claim"`
	Verdict             string   `json:"verdict"`
	FreshnessScore      int      `json:"freshness_score"`
	SuggestedCorrection string   `json:"suggested_correction"`
	Sources             []Source `json:"sources"`
}

type Source struct {
	URL      string `json:"url"`
	Title    string `json:"title"`
	Official bool   `json:"official"`
}

// Outcome is what the guard wants done to the HTTP response. An empty Action means "leave it alone".
type Outcome struct {
	Action  string
	Status  int
	Body    []byte
	Headers map[string]string
}

// Guard holds the config and the HTTP client used to reach the checker.
type Guard struct {
	cfg    Config
	client *http.Client
}

func NewGuard(cfg Config) *Guard {
	return &Guard{cfg: cfg, client: &http.Client{Timeout: time.Duration(cfg.TimeoutMS) * time.Millisecond}}
}

func headerValue(h map[string]string, name string) string {
	for k, v := range h {
		if strings.EqualFold(k, name) {
			return v
		}
	}
	return ""
}

// resolveMode applies the per-request x-freshness-guard header on top of the configured mode.
func (g *Guard) resolveMode(reqHeaders map[string]string) string {
	if v := strings.ToLower(strings.TrimSpace(headerValue(reqHeaders, headerControl))); v != "" {
		switch v {
		case "on", "true", "1":
			if g.cfg.Mode == ModeOff {
				return ModeAnnotate
			}
			return g.cfg.Mode
		case "false", "0":
			return ModeOff
		}
		if validMode(v) {
			return v
		}
	}
	return g.cfg.Mode
}

func skipped(reason string) Outcome {
	return Outcome{Action: "skipped:" + reason, Headers: map[string]string{"x-freshness-status": "skipped-" + reason}}
}

// Process decides what to do with one HTTP exchange. It never panics on unexpected JSON: anything it
// does not understand is passed through unchanged.
func (g *Guard) Process(reqHeaders map[string]string, path string, reqBody []byte, status int, respBody []byte) Outcome {
	if !strings.HasSuffix(strings.TrimRight(path, "/"), "/chat/completions") {
		return Outcome{} // not a chat completion: not our business
	}
	mode := g.resolveMode(reqHeaders)
	if mode == ModeOff {
		return Outcome{}
	}
	if status != http.StatusOK {
		return Outcome{}
	}
	var req map[string]any
	if json.Unmarshal(reqBody, &req) == nil {
		if s, ok := req["stream"].(bool); ok && s {
			return skipped("stream")
		}
	}
	var resp map[string]any
	if err := json.Unmarshal(respBody, &resp); err != nil {
		return skipped("unparsed")
	}
	message, answer := firstAnswer(resp)
	if message == nil || len(strings.TrimSpace(answer)) < g.cfg.MinChars {
		return skipped("short")
	}

	result, err := g.check(answer, mode != ModeAnnotate)
	if err != nil {
		if *g.cfg.FailOpen {
			resp["freshness"] = map[string]any{"status": "unchecked", "error": err.Error()}
			body, _ := json.Marshal(resp)
			return Outcome{Action: "fail-open", Body: body,
				Headers: map[string]string{"x-freshness-status": "unchecked"}}
		}
		body, _ := json.Marshal(map[string]any{"error": map[string]any{
			"type": "freshness_check_unavailable", "message": "Freshness Guard could not verify this answer: " + err.Error()}})
		return Outcome{Action: "fail-closed", Status: http.StatusServiceUnavailable, Body: body,
			Headers: map[string]string{"x-freshness-status": "unavailable"}}
	}

	summary := summarise(result, mode)
	headers := map[string]string{
		"x-freshness-status":      "checked",
		"x-freshness-stale-claims": strconv.Itoa(len(staleClaims(result))),
	}
	if result.OverallFreshness != nil {
		headers["x-freshness-score"] = strconv.Itoa(*result.OverallFreshness)
	}

	if mode == ModeBlock && result.HasStaleClaims && result.OverallFreshness != nil &&
		*result.OverallFreshness < g.cfg.MinScore {
		headers["x-freshness-status"] = "blocked"
		summary["original_answer"] = answer // kept for audit trails; the client never sees it as an answer
		body, _ := json.Marshal(map[string]any{"error": map[string]any{
			"type": "stale_answer",
			"message": fmt.Sprintf("Freshness Guard blocked this answer: freshness %d/100 is below the minimum of %d.",
				*result.OverallFreshness, g.cfg.MinScore),
			"freshness": summary,
		}})
		return Outcome{Action: "blocked", Status: http.StatusUnprocessableEntity, Body: body, Headers: headers}
	}

	action := "annotated"
	if mode == ModeFix && result.HasStaleClaims && result.CorrectedAnswer != nil && *result.CorrectedAnswer != "" {
		summary["original_answer"] = answer
		message["content"] = *result.CorrectedAnswer
		headers["x-freshness-status"] = "fixed"
		action = "fixed"
	}
	resp["freshness"] = summary
	body, err := json.Marshal(resp)
	if err != nil {
		return skipped("marshal")
	}
	return Outcome{Action: action, Body: body, Headers: headers}
}

// firstAnswer returns choices[0].message and its text (string content or concatenated text parts).
func firstAnswer(resp map[string]any) (map[string]any, string) {
	choices, ok := resp["choices"].([]any)
	if !ok || len(choices) == 0 {
		return nil, ""
	}
	choice, ok := choices[0].(map[string]any)
	if !ok {
		return nil, ""
	}
	message, ok := choice["message"].(map[string]any)
	if !ok {
		return nil, ""
	}
	switch c := message["content"].(type) {
	case string:
		return message, c
	case []any:
		var sb strings.Builder
		for _, part := range c {
			if p, ok := part.(map[string]any); ok {
				if t, ok := p["text"].(string); ok {
					sb.WriteString(t)
				}
			}
		}
		return message, sb.String()
	}
	return nil, ""
}

func (g *Guard) check(answer string, fix bool) (*CheckResult, error) {
	payload, _ := json.Marshal(map[string]any{"answer": answer, "fix": fix})
	req, err := http.NewRequest(http.MethodPost, g.cfg.CheckerURL, bytes.NewReader(payload))
	if err != nil {
		return nil, err
	}
	req.Header.Set("Content-Type", "application/json")
	res, err := g.client.Do(req)
	if err != nil {
		return nil, fmt.Errorf("checker unreachable: %w", err)
	}
	defer res.Body.Close()
	body, _ := io.ReadAll(io.LimitReader(res.Body, 4<<20))
	if res.StatusCode != http.StatusOK {
		return nil, fmt.Errorf("checker returned HTTP %d", res.StatusCode)
	}
	var out CheckResult
	if err := json.Unmarshal(body, &out); err != nil {
		return nil, fmt.Errorf("checker returned invalid JSON: %w", err)
	}
	return &out, nil
}

func staleClaims(r *CheckResult) []Claim {
	var out []Claim
	for _, c := range r.Claims {
		if c.Verdict == "Outdated" || c.Verdict == "Possibly stale" {
			out = append(out, c)
		}
	}
	return out
}

// summarise builds the compact "freshness" object added to the response.
func summarise(r *CheckResult, mode string) map[string]any {
	claims := make([]map[string]any, 0, len(r.Claims))
	for _, c := range r.Claims {
		srcs := make([]string, 0, len(c.Sources))
		official := 0
		for _, s := range c.Sources {
			srcs = append(srcs, s.URL)
			if s.Official {
				official++
			}
		}
		item := map[string]any{
			"claim": c.Claim, "verdict": c.Verdict, "freshness_score": c.FreshnessScore,
			"sources": srcs, "official_sources": official,
		}
		if c.SuggestedCorrection != "" {
			item["suggested_correction"] = c.SuggestedCorrection
		}
		claims = append(claims, item)
	}
	out := map[string]any{
		"status":           "checked",
		"mode":             mode,
		"overall_score":    r.OverallFreshness,
		"has_stale_claims": r.HasStaleClaims,
		"claims":           claims,
		"checked_at":       r.CheckedAt,
		"checker":          "policypulse",
	}
	if r.CorrectedAnswer != nil && *r.CorrectedAnswer != "" {
		out["corrected_answer"] = *r.CorrectedAnswer
	}
	return out
}
