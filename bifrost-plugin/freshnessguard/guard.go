package main

import (
	"bytes"
	"crypto/rand"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"io"
	mrand "math/rand"
	"net/http"
	"regexp"
	"strconv"
	"strings"
	"sync"
	"time"
)

// Modes the guard can run in. A request can override the configured mode with the
// x-freshness-guard header (off, annotate, fix, block, audit).
const (
	ModeOff      = "off"
	ModeAnnotate = "annotate" // wait for the check, attach verdicts
	ModeFix      = "fix"      // wait for the check, replace stale answers with the corrected text
	ModeBlock    = "block"    // wait for the check, return 422 for stale answers
	ModeAudit    = "audit"    // return at once, check in the background, report to the audit webhook

	headerControl = "x-freshness-guard"
	sourceName    = "bifrost-freshness-guard"
)

// Config is read from the plugin's "config" object in Bifrost's config.json.
type Config struct {
	CheckerURL        string   `json:"checker_url"`         // freshness checker endpoint (POST {answer, fix})
	Mode              string   `json:"mode"`                // default mode: annotate | fix | block | audit | off
	MinScore          int      `json:"min_score"`           // block mode: block when overall freshness < min_score
	TimeoutMS         int      `json:"timeout_ms"`          // how long to wait for the checker
	FailOpen          *bool    `json:"fail_open"`           // checker down: pass through (true) or return 503 (false)
	MinChars          int      `json:"min_chars"`           // skip very short answers
	OnlyTimeSensitive *bool    `json:"only_time_sensitive"` // skip answers with no dates, prices, versions, legal terms...
	SampleRate        *float64 `json:"sample_rate"`         // 0..1 share of eligible answers to check
	OnlyModels        []string `json:"only_models"`         // check only models with these prefixes
	SkipModels        []string `json:"skip_models"`         // never check models with these prefixes
	CacheTTLSeconds   *int     `json:"cache_ttl_seconds"`   // verdict cache lifetime; 0 disables the cache
	CacheMaxEntries   int      `json:"cache_max_entries"`   // verdict cache size
	MaxConcurrent     int      `json:"max_concurrent"`      // background (audit/stream) checks running at once
	AuditWebhookURL   string   `json:"audit_webhook_url"`   // optional: POST one JSON event per checked answer
}

func boolPtr(b bool) *bool { return &b }

func defaultConfig() Config {
	rate, ttl := 1.0, 21600
	return Config{
		CheckerURL:        "http://localhost:8000/check",
		Mode:              ModeAnnotate,
		MinScore:          40,
		TimeoutMS:         90000,
		FailOpen:          boolPtr(true),
		MinChars:          40,
		OnlyTimeSensitive: boolPtr(true),
		SampleRate:        &rate,
		CacheTTLSeconds:   &ttl,
		CacheMaxEntries:   1000,
		MaxConcurrent:     4,
	}
}

// parseConfig accepts whatever Bifrost passes to Init (usually map[string]any) and fills defaults.
func parseConfig(raw any) (Config, error) {
	cfg := defaultConfig()
	if raw != nil {
		b, err := json.Marshal(raw)
		if err != nil {
			return cfg, fmt.Errorf("freshness-guard: config is not JSON-serialisable: %w", err)
		}
		if err := json.Unmarshal(b, &cfg); err != nil {
			return cfg, fmt.Errorf("freshness-guard: invalid config: %w", err)
		}
	}
	def := defaultConfig()
	if cfg.FailOpen == nil {
		cfg.FailOpen = def.FailOpen
	}
	if cfg.OnlyTimeSensitive == nil {
		cfg.OnlyTimeSensitive = def.OnlyTimeSensitive
	}
	if cfg.SampleRate == nil {
		cfg.SampleRate = def.SampleRate
	}
	if cfg.CacheTTLSeconds == nil {
		cfg.CacheTTLSeconds = def.CacheTTLSeconds
	}
	if cfg.TimeoutMS <= 0 {
		cfg.TimeoutMS = def.TimeoutMS
	}
	if cfg.CacheMaxEntries <= 0 {
		cfg.CacheMaxEntries = def.CacheMaxEntries
	}
	if cfg.MaxConcurrent <= 0 {
		cfg.MaxConcurrent = def.MaxConcurrent
	}
	cfg.Mode = strings.ToLower(strings.TrimSpace(cfg.Mode))
	if !validMode(cfg.Mode) {
		return cfg, fmt.Errorf("freshness-guard: unknown mode %q (use annotate, fix, block, audit or off)", cfg.Mode)
	}
	if cfg.CheckerURL == "" {
		return cfg, fmt.Errorf("freshness-guard: checker_url is required")
	}
	if *cfg.SampleRate < 0 || *cfg.SampleRate > 1 {
		return cfg, fmt.Errorf("freshness-guard: sample_rate must be between 0 and 1")
	}
	return cfg, nil
}

func validMode(m string) bool {
	switch m {
	case ModeOff, ModeAnnotate, ModeFix, ModeBlock, ModeAudit:
		return true
	}
	return false
}

// CheckResult is the checker's reply to POST {answer, fix}. Any service that returns this shape can be used.
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

// AuditEvent is posted to audit_webhook_url for every answer the guard checks.
type AuditEvent struct {
	CheckID       string           `json:"check_id"`
	Time          string           `json:"time"`
	Source        string           `json:"source"`
	Model         string           `json:"model"`
	Mode          string           `json:"mode"`
	Action        string           `json:"action"` // annotated | fixed | blocked | audited | fail-open | fail-closed
	Stream        bool             `json:"stream"`
	Cache         string           `json:"cache"` // hit | miss
	CheckMS       int64            `json:"check_ms"`
	Score         *int             `json:"score"`
	StaleClaims   int              `json:"stale_claims"`
	ClaimsChecked int              `json:"claims_checked"`
	Claims        []map[string]any `json:"claims,omitempty"`
	AnswerExcerpt string           `json:"answer_excerpt"`
	Error         string           `json:"error,omitempty"`
}

type cacheEntry struct {
	result *CheckResult
	hasFix bool
	at     time.Time
}

// Guard holds the config, the checker client, the verdict cache and the background-check limiter.
type Guard struct {
	cfg    Config
	client *http.Client
	hook   *http.Client
	rand   func() float64
	now    func() time.Time

	mu    sync.Mutex
	cache map[string]cacheEntry

	sem chan struct{}
	wg  sync.WaitGroup
}

func NewGuard(cfg Config) *Guard {
	return &Guard{
		cfg:    cfg,
		client: &http.Client{Timeout: time.Duration(cfg.TimeoutMS) * time.Millisecond},
		hook:   &http.Client{Timeout: 10 * time.Second},
		rand:   mrand.Float64,
		now:    time.Now,
		cache:  map[string]cacheEntry{},
		sem:    make(chan struct{}, cfg.MaxConcurrent),
	}
}

// Wait blocks until background checks and webhook posts finish (used by Cleanup and tests).
func (g *Guard) Wait(timeout time.Duration) {
	done := make(chan struct{})
	go func() { g.wg.Wait(); close(done) }()
	select {
	case <-done:
	case <-time.After(timeout):
	}
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
// explicit is true when the caller chose a mode for this request.
func (g *Guard) resolveMode(reqHeaders map[string]string) (mode string, explicit bool) {
	if v := strings.ToLower(strings.TrimSpace(headerValue(reqHeaders, headerControl))); v != "" {
		switch v {
		case "on", "true", "1":
			if g.cfg.Mode == ModeOff {
				return ModeAnnotate, true
			}
			return g.cfg.Mode, true
		case "false", "0":
			return ModeOff, true
		}
		if validMode(v) {
			return v, true
		}
	}
	return g.cfg.Mode, false
}

func isChatPath(path string) bool {
	return strings.HasSuffix(strings.TrimRight(path, "/"), "/chat/completions")
}

func skipped(reason string) Outcome {
	return Outcome{Action: "skipped:" + reason, Headers: map[string]string{"x-freshness-status": "skipped-" + reason}}
}

// timeSensitive spots answers that can go stale: years, money, versions, recency words, legal and company terms.
var timeSensitive = regexp.MustCompile(`(?i)\b(19|20)\d{2}\b|[$€£₱¥]\s?\d|\b\d+(\.\d+)?\s?(usd|eur|php|percent)\b|\d\s?%` +
	`|\b(latest|newest|current|currently|recent|recently|as of|this year|upcoming|deadline|effective|in force|enacted` +
	`|adopted|proposed|pending|repealed|amended|ceo|chief executive|release|released|launched|announced|price|prices` +
	`|pricing|cost|costs|fee|fees|version|regulation|regulations|directive|act|law|laws|circular|guideline|guidelines` +
	`|compliance|penalty|penalties|fine|fines)\b|\bv\d+(\.\d+)+`)

func looksTimeSensitive(answer string) bool { return timeSensitive.MatchString(answer) }

func matchesAny(model string, prefixes []string) bool {
	m := strings.ToLower(model)
	for _, p := range prefixes {
		p = strings.ToLower(strings.TrimSuffix(strings.TrimSpace(p), "*"))
		if p != "" && strings.HasPrefix(m, p) {
			return true
		}
	}
	return false
}

// eligible applies the cheap filters shared by normal and streaming responses. It returns a skip reason or "".
func (g *Guard) eligible(answer, model string, explicit bool) string {
	if len(strings.TrimSpace(answer)) < g.cfg.MinChars {
		return "short"
	}
	if matchesAny(model, g.cfg.SkipModels) || (len(g.cfg.OnlyModels) > 0 && !matchesAny(model, g.cfg.OnlyModels)) {
		return "model"
	}
	if *g.cfg.OnlyTimeSensitive && !looksTimeSensitive(answer) {
		return "timeless"
	}
	if !explicit && *g.cfg.SampleRate < 1 && g.rand() >= *g.cfg.SampleRate {
		return "sampled"
	}
	return ""
}

func answerKey(answer string) string {
	sum := sha256.Sum256([]byte(strings.TrimSpace(answer)))
	return hex.EncodeToString(sum[:])
}

func newCheckID() string {
	b := make([]byte, 8)
	if _, err := rand.Read(b); err != nil {
		return strconv.FormatInt(time.Now().UnixNano(), 16)
	}
	return hex.EncodeToString(b)
}

func (g *Guard) cacheGet(key string, needFix bool) *CheckResult {
	if *g.cfg.CacheTTLSeconds <= 0 {
		return nil
	}
	g.mu.Lock()
	defer g.mu.Unlock()
	e, ok := g.cache[key]
	if !ok {
		return nil
	}
	if g.now().Sub(e.at) > time.Duration(*g.cfg.CacheTTLSeconds)*time.Second {
		delete(g.cache, key)
		return nil
	}
	if needFix && !e.hasFix && e.result.HasStaleClaims {
		return nil // cached verdicts, but no corrected answer yet
	}
	return e.result
}

func (g *Guard) cachePut(key string, r *CheckResult, hasFix bool) {
	if *g.cfg.CacheTTLSeconds <= 0 {
		return
	}
	g.mu.Lock()
	defer g.mu.Unlock()
	if len(g.cache) >= g.cfg.CacheMaxEntries {
		ttl := time.Duration(*g.cfg.CacheTTLSeconds) * time.Second
		for k, e := range g.cache { // drop expired entries, then one more if still full
			if g.now().Sub(e.at) > ttl {
				delete(g.cache, k)
			}
		}
		for k := range g.cache {
			if len(g.cache) < g.cfg.CacheMaxEntries {
				break
			}
			delete(g.cache, k)
		}
	}
	g.cache[key] = cacheEntry{result: r, hasFix: hasFix, at: g.now()}
}

// lookup returns the verdicts for an answer from the cache or the checker.
func (g *Guard) lookup(answer string, needFix bool) (r *CheckResult, cached bool, ms int64, err error) {
	key := answerKey(answer)
	if r = g.cacheGet(key, needFix); r != nil {
		return r, true, 0, nil
	}
	t0 := time.Now()
	r, err = g.check(answer, needFix)
	ms = time.Since(t0).Milliseconds()
	if err != nil {
		return nil, false, ms, err
	}
	g.cachePut(key, r, needFix)
	return r, false, ms, nil
}

func requestModel(reqBody []byte, resp map[string]any) (model string, stream bool) {
	var req map[string]any
	if json.Unmarshal(reqBody, &req) == nil {
		model, _ = req["model"].(string)
		stream, _ = req["stream"].(bool)
	}
	if model == "" && resp != nil {
		model, _ = resp["model"].(string)
	}
	return model, stream
}

// Process decides what to do with one non-streaming HTTP exchange. Anything it does not understand is
// passed through unchanged.
func (g *Guard) Process(reqHeaders map[string]string, path string, reqBody []byte, status int, respBody []byte) Outcome {
	return g.ProcessID("", reqHeaders, path, reqBody, status, respBody)
}

// ProcessID is Process with a caller-supplied check ID (Bifrost's request ID), so audit events can be
// matched to the gateway's own logs. An empty ID gets a random one.
func (g *Guard) ProcessID(checkID string, reqHeaders map[string]string, path string, reqBody []byte, status int, respBody []byte) Outcome {
	if !isChatPath(path) {
		return Outcome{}
	}
	mode, explicit := g.resolveMode(reqHeaders)
	if mode == ModeOff || status != http.StatusOK {
		return Outcome{}
	}
	var resp map[string]any
	if err := json.Unmarshal(respBody, &resp); err != nil {
		if _, stream := requestModel(reqBody, nil); stream {
			return Outcome{} // streamed responses are handled by the stream hook
		}
		return skipped("unparsed")
	}
	model, stream := requestModel(reqBody, resp)
	if stream {
		return Outcome{}
	}
	message, answer := firstAnswer(resp)
	if message == nil {
		return skipped("short")
	}
	if reason := g.eligible(answer, model, explicit); reason != "" {
		return skipped(reason)
	}
	if checkID == "" {
		checkID = newCheckID()
	}

	if mode == ModeAudit {
		if r := g.cacheGet(answerKey(answer), false); r != nil { // known answer: annotate at no cost
			return g.annotated(resp, r, mode, checkID, model, answer, true, 0)
		}
		if !g.background(func() { g.auditCheck(checkID, model, mode, answer, false) }) {
			return skipped("busy")
		}
		return Outcome{Action: "audit-pending", Headers: map[string]string{
			"x-freshness-status": "pending", "x-freshness-check-id": checkID}}
	}

	result, cached, ms, err := g.lookup(answer, mode == ModeFix)
	if err != nil {
		ev := g.event(checkID, model, mode, answer, nil, false, ms)
		ev.Error = err.Error()
		if *g.cfg.FailOpen {
			ev.Action = "fail-open"
			g.emit(ev)
			resp["freshness"] = map[string]any{"status": "unchecked", "error": err.Error()}
			body, _ := json.Marshal(resp)
			return Outcome{Action: "fail-open", Body: body, Headers: map[string]string{
				"x-freshness-status": "unchecked", "x-freshness-check-id": checkID}}
		}
		ev.Action = "fail-closed"
		g.emit(ev)
		body, _ := json.Marshal(map[string]any{"error": map[string]any{
			"type": "freshness_check_unavailable", "message": "Freshness Guard could not verify this answer: " + err.Error()}})
		return Outcome{Action: "fail-closed", Status: http.StatusServiceUnavailable, Body: body,
			Headers: map[string]string{"x-freshness-status": "unavailable", "x-freshness-check-id": checkID}}
	}

	summary := summarise(result, mode)
	headers := resultHeaders(result, checkID, cached)
	ev := g.event(checkID, model, mode, answer, result, cached, ms)

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
		ev.Action = "blocked"
		g.emit(ev)
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
	ev.Action = action
	g.emit(ev)
	return Outcome{Action: action, Body: body, Headers: headers}
}

func (g *Guard) annotated(resp map[string]any, r *CheckResult, mode, checkID, model, answer string, cached bool, ms int64) Outcome {
	resp["freshness"] = summarise(r, mode)
	body, err := json.Marshal(resp)
	if err != nil {
		return skipped("marshal")
	}
	ev := g.event(checkID, model, mode, answer, r, cached, ms)
	ev.Action = "annotated"
	g.emit(ev)
	return Outcome{Action: "annotated", Body: body, Headers: resultHeaders(r, checkID, cached)}
}

// StreamPlan says whether a streaming chat request will be audited. The request body is only available
// when the response headers are written, so this is called from the headers hook.
func (g *Guard) StreamPlan(reqHeaders map[string]string, path string, reqBody []byte) bool {
	if !isChatPath(path) {
		return false
	}
	if mode, _ := g.resolveMode(reqHeaders); mode == ModeOff {
		return false
	}
	_, stream := requestModel(reqBody, nil)
	return stream
}

// AuditStream checks a fully streamed answer in the background. The text has already reached the client,
// so every mode behaves like audit here. It returns what it did, for logs and tests.
func (g *Guard) AuditStream(mode string, explicit bool, model, answer, checkID string) string {
	if reason := g.eligible(answer, model, explicit); reason != "" {
		return "skipped:" + reason
	}
	if checkID == "" {
		checkID = newCheckID()
	}
	if !g.background(func() { g.auditCheck(checkID, model, mode, answer, true) }) {
		return "skipped:busy"
	}
	return "audit-pending"
}

// background runs fn if a slot is free. It never blocks the request path.
func (g *Guard) background(fn func()) bool {
	select {
	case g.sem <- struct{}{}:
	default:
		return false
	}
	g.wg.Add(1)
	go func() {
		defer g.wg.Done()
		defer func() { <-g.sem }()
		fn()
	}()
	return true
}

func (g *Guard) auditCheck(checkID, model, mode, answer string, stream bool) {
	result, cached, ms, err := g.lookup(answer, false)
	ev := g.event(checkID, model, mode, answer, result, cached, ms)
	ev.Stream = stream
	ev.Action = "audited"
	if err != nil {
		ev.Action, ev.Error = "fail-open", err.Error()
	}
	g.post(ev)
}

func (g *Guard) event(checkID, model, mode, answer string, r *CheckResult, cached bool, ms int64) AuditEvent {
	excerpt := strings.TrimSpace(answer)
	if len(excerpt) > 400 {
		excerpt = excerpt[:400] + "..."
	}
	ev := AuditEvent{CheckID: checkID, Time: g.now().UTC().Format(time.RFC3339), Source: sourceName, Model: model,
		Mode: mode, Cache: "miss", CheckMS: ms, AnswerExcerpt: excerpt}
	if cached {
		ev.Cache = "hit"
	}
	if r != nil {
		ev.Score = r.OverallFreshness
		ev.StaleClaims = len(staleClaims(r))
		ev.ClaimsChecked = len(r.Claims)
		for _, c := range r.Claims {
			ev.Claims = append(ev.Claims, map[string]any{"claim": c.Claim, "verdict": c.Verdict,
				"freshness_score": c.FreshnessScore})
		}
	}
	return ev
}

// emit posts an audit event without holding up the response.
func (g *Guard) emit(ev AuditEvent) {
	if g.cfg.AuditWebhookURL == "" {
		return
	}
	g.wg.Add(1)
	go func() {
		defer g.wg.Done()
		g.post(ev)
	}()
}

func (g *Guard) post(ev AuditEvent) {
	if g.cfg.AuditWebhookURL == "" {
		return
	}
	payload, err := json.Marshal(ev)
	if err != nil {
		return
	}
	res, err := g.hook.Post(g.cfg.AuditWebhookURL, "application/json", bytes.NewReader(payload))
	if err == nil {
		_, _ = io.Copy(io.Discard, io.LimitReader(res.Body, 1<<16))
		res.Body.Close()
	}
}

func resultHeaders(r *CheckResult, checkID string, cached bool) map[string]string {
	h := map[string]string{
		"x-freshness-status":       "checked",
		"x-freshness-stale-claims": strconv.Itoa(len(staleClaims(r))),
		"x-freshness-check-id":     checkID,
		"x-freshness-cache":        "miss",
	}
	if cached {
		h["x-freshness-cache"] = "hit"
	}
	if r.OverallFreshness != nil {
		h["x-freshness-score"] = strconv.Itoa(*r.OverallFreshness)
	}
	return h
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
	}
	if r.CorrectedAnswer != nil && *r.CorrectedAnswer != "" {
		out["corrected_answer"] = *r.CorrectedAnswer
	}
	return out
}
