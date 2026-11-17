# Freshness Guard (Bifrost plugin)

A native Bifrost plugin that checks chat completion answers for stale claims: regulations, prices, API and
model versions, company facts. It runs inside the gateway, so every app behind Bifrost is covered without
code changes.

## What it does for an operator

| Problem | What the plugin does |
|---|---|
| Models answer from old training data and present it as current | Sends each answer to a freshness checker and annotates, fixes or blocks it |
| A live check takes seconds, and most traffic can't wait | `audit` mode returns the answer at once and checks in the background |
| Checking every answer is expensive | Smart skip (only answers with dates, prices, versions, legal or company terms), sampling, per-model filters, and a verdict cache |
| Streaming responses can't be held back | Streamed answers are collected chunk by chunk and audited when the stream ends |
| "Which of my models go stale?" | Every check is posted to an audit webhook with the model, score and Bifrost request ID |
| The checker might be down | Fail-open by default; background checks are bounded and never block requests |

## Bifrost extension points used

| Hook | Use |
|---|---|
| `Init(config any)` / `GetName()` / `Cleanup()` | Lifecycle; `Cleanup` lets background checks report before shutdown |
| `HTTPTransportPostHook` | Non-streaming chat completions: annotate, fix, block or hand off to a background check |
| `HTTPTransportResponseHeadersHook` | Marks streamed responses `x-freshness-status: pending` with the check ID |
| `HTTPTransportStreamChunkHook` | Collects streamed text (chunks pass through untouched) and audits it at the end |

Two details of Bifrost v2.5.1 shaped the stream handling: each chunk arrives with a fresh context, and the
request body is not passed to the chunk hook. Stream state is therefore keyed by Bifrost's request ID
(`BifrostContextKeyRequestID`), which is also used as the check ID so audit events line up with Bifrost's logs.

## Modes

| Mode | Added latency | What the client receives |
|---|---|---|
| `audit` | none | The untouched answer with `x-freshness-status: pending` and `x-freshness-check-id`. The result goes to the audit webhook. Answers already in the cache come back annotated at once |
| `annotate` | one check | The answer plus a `freshness` object and score headers |
| `fix` | one check | The answer with stale parts corrected; the original in `freshness.original_answer` |
| `block` | one check | HTTP 422 `stale_answer` when the score is below `min_score` |
| `off` | none | Untouched |

Set the default in config. Any request can choose its own with the `x-freshness-guard` header. Streamed
responses are always audited (the text has already left the gateway).

Response headers: `x-freshness-status` (`checked`, `fixed`, `blocked`, `pending`, `unchecked`, `unavailable`,
`skipped-short|timeless|sampled|model|busy|unparsed`), `x-freshness-score`, `x-freshness-stale-claims`,
`x-freshness-cache` (`hit` or `miss`), `x-freshness-check-id`.

## Configuration

```json
{
  "enabled": true,
  "name": "freshness-guard",
  "path": "/app/plugins/freshness_guard.so",
  "config": {
    "checker_url": "http://checker:8000/check",
    "audit_webhook_url": "http://checker:8000/audit",
    "mode": "audit"
  }
}
```

| Key | Default | Meaning |
|---|---|---|
| `checker_url` | `http://localhost:8000/check` | Freshness checker endpoint (contract below) |
| `mode` | `annotate` | `audit`, `annotate`, `fix`, `block` or `off` |
| `min_score` | `40` | `block` mode: block stale answers scoring below this |
| `timeout_ms` | `90000` | How long to wait for the checker |
| `fail_open` | `true` | Checker unavailable: pass the answer through (`true`) or return 503 (`false`) |
| `min_chars` | `40` | Skip answers shorter than this |
| `only_time_sensitive` | `true` | Skip answers with no dates, money, versions, recency, legal or company terms |
| `sample_rate` | `1.0` | Share of eligible answers to check (an explicit request header bypasses sampling) |
| `only_models` / `skip_models` | none | Model prefixes to include or exclude, for example `["groq/"]` |
| `cache_ttl_seconds` | `21600` | How long verdicts are reused for identical answers; `0` disables the cache |
| `cache_max_entries` | `1000` | Verdict cache size |
| `max_concurrent` | `4` | Background checks running at once; extra ones are skipped (`skipped-busy`), never queued |
| `audit_webhook_url` | none | POST one JSON event per checked answer |

## Checker contract

The plugin is not tied to one checker. Any HTTP service that implements this works:

```http
POST {checker_url}
{"answer": "<model answer>", "fix": true}

200 OK
{"overall_freshness": 13, "has_stale_claims": true, "corrected_answer": "...", "checked_at": "...",
 "claims": [{"claim": "...", "verdict": "Outdated", "freshness_score": 10, "suggested_correction": "...",
             "sources": [{"url": "https://...", "title": "...", "official": true}]}]}
```

`verdict` is one of `Current`, `Possibly stale`, `Outdated`, `Unverifiable`. `corrected_answer` is only needed
when `fix` is true. This repository's checker is PolicyPulse (`api.py`), which verifies claims live with Tavily.

## Audit event

Posted to `audit_webhook_url` for every checked answer:

```json
{"check_id": "f6327b30-eda8-4d0c-9437-89d52af63f16", "time": "2026-11-17T16:50:12Z",
 "source": "bifrost-freshness-guard", "model": "groq/openai/gpt-oss-120b", "mode": "audit",
 "action": "audited", "stream": true, "cache": "miss", "check_ms": 41250, "score": 13,
 "stale_claims": 3, "claims_checked": 3, "claims": [{"claim": "...", "verdict": "Outdated", "freshness_score": 10}],
 "answer_excerpt": "The EU AI Act entered into force on 6 July 2023..."}
```

`action` is `annotated`, `fixed`, `blocked`, `audited`, `fail-open` or `fail-closed`. `check_id` is Bifrost's
request ID when Bifrost provides one.

## Build

Go plugins load only into a dynamically linked Bifrost built with the same Go version and dependency versions
(Bifrost's documented custom-plugin route). `docker/bifrost.Dockerfile` in this repository does it in one step:
it builds `bifrost-http` v2.5.1 without `-static`, then vets, tests and builds this plugin inside Bifrost's own
`transports` module so the two always match.

```bash
make test          # go vet + go test -race (in a golang:1.27.3 container)
make image         # Bifrost v2.5.1 (dynamic) + freshness_guard.so
```

To build against a local Bifrost checkout instead, copy `main.go` and `guard.go` into
`transports/freshnessguard/` and run `go build -buildmode=plugin -o freshness_guard.so ./freshnessguard`.

## Tests

- `guard_test.go`: 21 unit tests. All modes, the per-request header, cache hits, expiry and disabling, smart
  skip, sampling, model filters, bounded background checks, fail-open and fail-closed, webhook events, config
  validation, and the three hooks driven with Bifrost's own `schemas` types (including two interleaved streams).
- `scripts/e2e_test.py` (repository root): 29 checks against a real Bifrost with the plugin loaded, run in CI
  on every push with no API keys.

## Proposed upstream shape

For a pull request to `maximhq/bifrost`, this directory maps onto `plugins/freshnessguard/` (or
`examples/plugins/freshness-guard/` as a reference plugin): `main.go`, `guard.go`, `guard_test.go`, `go.mod`
and this README. It depends only on `core/schemas` and the standard library. The checker stays external,
behind the contract above, so the plugin carries no model, search or vendor dependency.
