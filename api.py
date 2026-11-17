"""PolicyPulse checker API: the service the Freshness Guard plugin calls.

Run:  uvicorn api:app --port 8000
  POST /check          {"answer": "...", "fix": true}  -> verdicts, scores, corrected answer
  POST /audit          audit event from the plugin's webhook
  GET  /audit          recent audit events          GET /audit/{check_id}   one event
  GET  /audit/stats    freshness by model

Replay mode (keyless demo and CI): set POLICYPULSE_REPLAY to a JSON file of recorded results keyed by the
SHA-256 of the answer. Recorded answers are served without calling any LLM or Tavily.
"""
import hashlib
import json
import os
import threading
from collections import defaultdict
from pathlib import Path

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

import config
from llm import GatewayError

app = FastAPI(title="PolicyPulse API", version="1.1",
              description="Stale-answer checker: verdicts and freshness scores for time-sensitive claims.")

AUDIT_LOG = config.LOGS / "audit.jsonl"
_lock = threading.Lock()
_replay_path = os.getenv("POLICYPULSE_REPLAY", "").strip()
REPLAY: dict = json.loads(Path(_replay_path).read_text(encoding="utf-8")) if _replay_path else {}
REPLAY_ONLY = os.getenv("POLICYPULSE_REPLAY_ONLY", "").strip() in ("1", "true", "yes")


def answer_key(answer: str) -> str:
    return hashlib.sha256(answer.strip().encode("utf-8")).hexdigest()


class CheckRequest(BaseModel):
    answer: str = Field(..., min_length=1, max_length=20000, description="The LLM answer to check")
    fix: bool = Field(True, description="Also return the answer rewritten with corrections")


@app.get("/health")
def health():
    return {"ok": True, "bifrost_url": config.BIFROST_URL, "primary_model": config.PRIMARY_MODEL,
            "fallback_model": config.FALLBACK_MODEL, "tavily_key_set": bool(config.TAVILY_API_KEY),
            "replay_answers": len(REPLAY), "replay_only": REPLAY_ONLY}


@app.post("/check")
def check(req: CheckRequest):
    recorded = REPLAY.get(answer_key(req.answer))
    if recorded:
        out = dict(recorded, replayed=True)
        if not req.fix:
            out["corrected_answer"] = None
        return out
    if REPLAY_ONLY:
        raise HTTPException(status_code=503, detail="Replay mode: this answer has no recorded check.")
    from pipeline import check_answer  # imported here so replay mode needs no LLM or Tavily setup
    try:
        report = check_answer(req.answer, fix=req.fix)
    except GatewayError as e:
        raise HTTPException(status_code=502, detail=f"LLM gateway error: {e}") from e
    stale = [r for r in report["results"] if r["verdict"] in ("Outdated", "Possibly stale")]
    return {"overall_freshness": report["overall"], "has_stale_claims": bool(stale),
            "corrected_answer": report["fixed_answer"], "claims": report["results"],
            "checked_at": report["checked_at"]}


# ---------- audit log (fed by the plugin's audit_webhook_url) ----------
def _read_events() -> list[dict]:
    if not AUDIT_LOG.exists():
        return []
    out = []
    for line in AUDIT_LOG.read_text(encoding="utf-8").splitlines():
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            pass
    return out


@app.post("/audit")
def audit_add(event: dict):
    if not event.get("check_id"):
        raise HTTPException(status_code=422, detail="check_id is required")
    with _lock:
        with AUDIT_LOG.open("a", encoding="utf-8") as f:
            f.write(json.dumps(event, ensure_ascii=False) + "\n")
    return {"ok": True}


@app.get("/audit")
def audit_list(limit: int = 200):
    return _read_events()[::-1][: max(1, min(limit, 1000))]


@app.delete("/audit")
def audit_clear():
    with _lock:
        AUDIT_LOG.unlink(missing_ok=True)
    return {"ok": True}


@app.get("/audit/stats")
def audit_stats():
    by_model: dict[str, list[dict]] = defaultdict(list)
    for e in _read_events():
        by_model[e.get("model") or "unknown"].append(e)
    rows = []
    for model, evs in by_model.items():
        scores = [e["score"] for e in evs if e.get("score") is not None]
        rows.append({
            "model": model,
            "answers_checked": len(evs),
            "avg_freshness": round(sum(scores) / len(scores)) if scores else None,
            "stale_answers": sum(1 for e in evs if e.get("stale_claims", 0) > 0),
            "blocked": sum(1 for e in evs if e.get("action") == "blocked"),
            "fixed": sum(1 for e in evs if e.get("action") == "fixed"),
            "cache_hits": sum(1 for e in evs if e.get("cache") == "hit"),
            "streamed": sum(1 for e in evs if e.get("stream")),
        })
    rows.sort(key=lambda r: (r["avg_freshness"] is None, r["avg_freshness"]))
    return rows


@app.get("/audit/{check_id}")
def audit_get(check_id: str):
    for e in reversed(_read_events()):
        if e.get("check_id") == check_id:
            return e
    raise HTTPException(status_code=404, detail="No audit event with this check_id yet")
