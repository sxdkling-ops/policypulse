"""Compare a claim with live sources and return a verdict + freshness score."""
import json
from datetime import date

from llm import chat, parse_json

VERDICTS = ["Current", "Possibly stale", "Outdated", "Unverifiable"]

SYSTEM = """You are a careful fact-freshness judge. Compare the CLAIM with the SOURCES (live web results).
Rules:
- Use ONLY the sources. Never invent facts. If the sources do not clearly support a verdict, use "Unverifiable".
- "Current": sources confirm the claim is true today.
- "Possibly stale": partly true, or true earlier but sources suggest changes/newer developments.
- "Outdated": sources clearly show the claim is no longer true.
- freshness_score: 0-100 (100 = fully current, 0 = clearly outdated, ~50 when unverifiable).
- suggested_correction: a corrected sentence based on the sources, or "" if Current/Unverifiable.
- evidence: 1-2 sentences citing what the sources say.
Return ONLY JSON:
{"verdict": "Current|Possibly stale|Outdated|Unverifiable", "freshness_score": 0, "evidence": "",
 "suggested_correction": "", "source_urls": ["urls you relied on"]}"""


def judge_claim(claim: dict, sources: list[dict]) -> tuple[dict, dict]:
    """Returns (verdict_dict, llm_meta)."""
    if not sources:
        return {"verdict": "Unverifiable", "freshness_score": 50, "evidence": "No live sources found.",
                "suggested_correction": "", "sources": []}, {}
    src_text = "\n\n".join(
        f"[{i + 1}] {s['title']} ({s.get('published_date') or 'date n/a'})\n{s['url']}\n{s['snippet']}"
        for i, s in enumerate(sources))
    res = chat(f"Today is {date.today().isoformat()}.\n\nCLAIM: {claim['claim']}\n"
               f"Implied date: {claim.get('implied_date')}\n\nSOURCES:\n{src_text}",
               system=SYSTEM, max_tokens=1200, purpose="judge")
    try:
        v = parse_json(res["text"])
    except ValueError:
        v = {}
    verdict = v.get("verdict") if v.get("verdict") in VERDICTS else "Unverifiable"
    try:
        score = max(0, min(100, int(v.get("freshness_score", 50))))
    except (TypeError, ValueError):
        score = 50
    used = set(v.get("source_urls") or [])
    return {
        "verdict": verdict,
        "freshness_score": score,
        "evidence": v.get("evidence", ""),
        "suggested_correction": v.get("suggested_correction", ""),
        "sources": [s for s in sources if s["url"] in used] or sources[:3],
    }, res


def overall_score(results: list[dict]) -> int | None:
    scores = [r["freshness_score"] for r in results if r["verdict"] != "Unverifiable"]
    return round(sum(scores) / len(scores)) if scores else None
