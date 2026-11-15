"""PolicyPulse as an API, so any AI app can check an answer before showing it.

Run:  uvicorn api:app --port 8000
Call: POST /check  {"answer": "The EU AI Act is still a proposal.", "fix": true}
"""
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

import config
from llm import GatewayError
from pipeline import check_answer

app = FastAPI(title="PolicyPulse API", version="1.0",
              description="Stale-answer detector: verdicts and freshness scores for time-sensitive claims.")


class CheckRequest(BaseModel):
    answer: str = Field(..., min_length=1, max_length=8000, description="The LLM answer to check")
    fix: bool = Field(True, description="Also return the answer rewritten with corrections")


@app.get("/health")
def health():
    return {"ok": True, "bifrost_url": config.BIFROST_URL, "primary_model": config.PRIMARY_MODEL,
            "fallback_model": config.FALLBACK_MODEL, "tavily_key_set": bool(config.TAVILY_API_KEY)}


@app.post("/check")
def check(req: CheckRequest):
    try:
        report = check_answer(req.answer, fix=req.fix)
    except GatewayError as e:
        raise HTTPException(status_code=502, detail=f"LLM gateway error: {e}") from e
    stale = [r for r in report["results"] if r["verdict"] in ("Outdated", "Possibly stale")]
    return {"overall_freshness": report["overall"], "has_stale_claims": bool(stale),
            "corrected_answer": report["fixed_answer"], "claims": report["results"],
            "checked_at": report["checked_at"]}
