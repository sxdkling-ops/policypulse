"""End-to-end test of the Freshness Guard plugin running inside a real Bifrost gateway.

Runs against the keyless demo stack (docker compose -f docker-compose.demo.yml up --build), where a mock model
replays recorded answers and the checker replays the recorded live checks. Standard library only.

Usage: python scripts/e2e_test.py [bifrost_url] [checker_url]
Exit code 0 when every check passes.
"""
import json
import sys
import time
import urllib.error
import urllib.request

BIFROST = (sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8080").rstrip("/")
CHECKER = (sys.argv[2] if len(sys.argv) > 2 else "http://localhost:8000").rstrip("/")
MODEL = "openai/mock-2024"
Q_EU = "When did the EU AI Act enter into force? Answer in 2 sentences."            # stale: wrong date
Q_ANTHROPIC = "What is Anthropic's newest Claude model and what does its API cost?"  # stale: old model, old price
Q_MICROSOFT = "Who is the CEO of Microsoft and since when? Answer in 2 sentences."   # current
Q_TIMELESS = "Explain in 2 sentences what an API gateway does."                      # nothing to check

failures = []


def http(method, url, payload=None, headers=None, timeout=120):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"Content-Type": "application/json", **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, {k.lower(): v for k, v in r.headers.items()}, r.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        return e.code, {k.lower(): v for k, v in e.headers.items()}, e.read().decode("utf-8")


def chat(prompt, mode=None, stream=False):
    headers = {"x-freshness-guard": mode} if mode else {}
    body = {"model": MODEL, "messages": [{"role": "user", "content": prompt}]}
    if stream:
        body["stream"] = True
    t0 = time.perf_counter()
    status, hdrs, text = http("POST", f"{BIFROST}/v1/chat/completions", body, headers)
    ms = round((time.perf_counter() - t0) * 1000)
    return status, hdrs, text, ms


def check(name, ok, detail=""):
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"  ({detail})" if detail else ""))
    if not ok:
        failures.append(name)


def content(text):
    return json.loads(text)["choices"][0]["message"]["content"]


def wait_for(url, label, seconds=180):
    for _ in range(seconds // 2):
        try:
            if http("GET", url, timeout=5)[0] == 200:
                return
        except Exception:
            pass
        time.sleep(2)
    sys.exit(f"{label} did not become ready at {url}")


def audit_event(check_id, seconds=60):
    for _ in range(seconds):
        status, _, text = http("GET", f"{CHECKER}/audit/{check_id}")
        if status == 200:
            return json.loads(text)
        time.sleep(1)
    return None


wait_for(f"{CHECKER}/health", "checker")
wait_for(f"{BIFROST}/health", "Bifrost")
http("DELETE", f"{CHECKER}/audit")

print("1. annotate: answer passes through with verdicts attached")
status, h, text, _ = chat(Q_EU, "annotate")
body = json.loads(text)
original = content(text)
check("HTTP 200", status == 200, str(status))
check("x-freshness-status: checked", h.get("x-freshness-status") == "checked", str(h.get("x-freshness-status")))
check("first check is a cache miss", h.get("x-freshness-cache") == "miss")
check("freshness object flags stale claims", body.get("freshness", {}).get("has_stale_claims") is True)
check("score header matches body", h.get("x-freshness-score") == str(body["freshness"]["overall_score"]))

print("2. cache: the same answer is not checked twice")
status, h, text, _ = chat(Q_EU, "annotate")
check("x-freshness-cache: hit", h.get("x-freshness-cache") == "hit", str(h.get("x-freshness-cache")))
check("answer unchanged in annotate mode", content(text) == original)

print("3. fix: stale parts are corrected")
status, h, text, _ = chat(Q_EU, "fix")
body = json.loads(text)
check("HTTP 200 and status fixed", status == 200 and h.get("x-freshness-status") == "fixed",
      f"{status} {h.get('x-freshness-status')}")
check("answer was rewritten", content(text) != original)
check("corrected answer has the right date", "1 August 2024" in content(text))
check("original kept for audit", body["freshness"].get("original_answer") == original)

print("4. block: stale answers never reach the app")
status, h, text, _ = chat(Q_EU, "block")
body = json.loads(text)
check("HTTP 422", status == 422, str(status))
check("error type stale_answer", body.get("error", {}).get("type") == "stale_answer")
check("no choices in a blocked response", "choices" not in body)
status, h, text, _ = chat(Q_MICROSOFT, "block")
check("a current answer passes block mode", status == 200 and h.get("x-freshness-status") == "checked",
      f"{status} {h.get('x-freshness-status')} score={h.get('x-freshness-score')}")

print("5. smart skip: answers with nothing time-sensitive cost nothing")
status, h, text, _ = chat(Q_TIMELESS, "annotate")
check("x-freshness-status: skipped-timeless", h.get("x-freshness-status") == "skipped-timeless",
      str(h.get("x-freshness-status")))
check("body untouched", "freshness" not in json.loads(text))

print("6. off: the plugin stays out of the way")
status, h, text, _ = chat(Q_EU, "off")
check("no freshness headers", status == 200 and "x-freshness-status" not in h)

print("7. audit (the configured default): answer returns at once, check runs in the background")
status, h, text, ms = chat(Q_ANTHROPIC)  # no header: the gateway's configured mode applies
check("HTTP 200 and status pending", status == 200 and h.get("x-freshness-status") == "pending",
      f"{status} {h.get('x-freshness-status')}")
check("body untouched", "freshness" not in json.loads(text))
check("check ID is the Bifrost request ID", h.get("x-freshness-check-id") == h.get("x-bifrost-request-id"),
      str(h.get("x-freshness-check-id")))
ev = audit_event(h.get("x-freshness-check-id", "missing"))
check("audit event arrives", ev is not None)
check("audit event has a score and stale claims", bool(ev) and ev.get("score") is not None and ev.get("stale_claims", 0) > 0,
      json.dumps({k: ev.get(k) for k in ("action", "score", "stale_claims")}) if ev else "")

print("8. streaming: streamed answers are audited after the stream ends")
status, h, text, _ = chat(Q_EU + " (stream)", "audit", stream=True)
chunks = [ln for ln in text.splitlines() if ln.startswith("data: ") and ln != "data: [DONE]"]
streamed = "".join(json.loads(c[6:])["choices"][0].get("delta", {}).get("content") or "" for c in chunks
                   if json.loads(c[6:]).get("choices"))
check("stream delivered intact", status == 200 and streamed.strip() == original.strip(), f"{len(chunks)} chunks")
check("stream marked pending", h.get("x-freshness-status") == "pending", str(h.get("x-freshness-status")))
stream_id = h.get("x-freshness-check-id", "missing")
check("stream check ID is the Bifrost request ID", stream_id == h.get("x-bifrost-request-id"), stream_id)
ev = audit_event(stream_id)
check("stream audit event arrives", bool(ev) and ev.get("stream") is True and ev.get("action") == "audited",
      json.dumps({k: ev.get(k) for k in ("action", "stream", "cache", "score")}) if ev else "")

print("9. operator view: audit stats by model")
time.sleep(1)
status, _, text = http("GET", f"{CHECKER}/audit/stats")
stats = json.loads(text)
row = next((r for r in stats if r["model"] == MODEL), None)
check("stats row for the model", row is not None)
check("blocked, fixed and cache hits are counted",
      bool(row) and row["blocked"] >= 1 and row["fixed"] >= 1 and row["cache_hits"] >= 1 and row["streamed"] >= 1,
      json.dumps(row))

print()
if failures:
    print(f"FAILED: {len(failures)} check(s): {failures}")
    sys.exit(1)
print("ALL CHECKS PASSED")
