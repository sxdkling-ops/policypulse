"""Compare a claim with live sources: the LLM picks the verdict, the score is computed and explained."""
from datetime import date

from authority import freshness_score
from llm import chat, parse_json

VERDICTS = ["Current", "Possibly stale", "Outdated", "Unverifiable"]

SYSTEM = """You are a careful fact-freshness judge. Compare the CLAIM with the SOURCES (live web results).
Sources marked OFFICIAL come from the regulator, standards body or vendor itself; prefer them when sources disagree.
Rules:
- Use ONLY the sources. Never invent facts. If the sources do not clearly support a verdict, use "Unverifiable".
- "Current": sources confirm the claim is true today.
- "Possibly stale": partly true, or true earlier but sources suggest changes/newer developments.
- "Outdated": sources clearly show the claim is no longer true.
- supporting_urls: ONLY the URLs whose content directly supports your verdict.
- suggested_correction: a corrected sentence based on the sources, or "" if Current/Unverifiable.
- evidence: 1-2 sentences citing what the sources say.
Return ONLY JSON:
{"verdict": "Current|Possibly stale|Outdated|Unverifiable", "evidence": "", "suggested_correction": "",
 "supporting_urls": []}"""


def judge_claim(claim: dict, sources: list[dict]) -> tuple[dict, dict]:
    """Returns (verdict_dict, llm_meta)."""
    if not sources:
        return {"verdict": "Unverifiable", "freshness_score": 50, "score_parts": [("No live sources found", 50)],
                "evidence": "No live sources found.", "suggested_correction": "", "sources": []}, {}
    src_text = "\n\n".join(
        f"[{i + 1}]{' OFFICIAL' if s.get('official') else ''} {s['title']} ({s.get('published_date') or 'date n/a'})"
        f"\n{s['url']}\n{s['snippet']}" for i, s in enumerate(sources))
    res = chat(f"Today is {date.today().isoformat()}.\n\nCLAIM: {claim['claim']}\n"
               f"Implied date: {claim.get('implied_date')}\n\nSOURCES:\n{src_text}",
               system=SYSTEM, max_tokens=1200, purpose="judge")
    try:
        v = parse_json(res["text"])
        if isinstance(v, list):
            v = v[0] if v and isinstance(v[0], dict) else {}
    except ValueError:
        v = {}
    verdict = v.get("verdict") if v.get("verdict") in VERDICTS else "Unverifiable"
    used = set(v.get("supporting_urls") or v.get("source_urls") or [])
    supporting = [s for s in sources if s["url"] in used]
    score, parts = freshness_score(verdict, supporting)
    return {
        "verdict": verdict,
        "freshness_score": score,
        "score_parts": parts,
        "evidence": v.get("evidence", ""),
        "suggested_correction": v.get("suggested_correction", "") if verdict in ("Outdated", "Possibly stale") else "",
        "sources": supporting or sources[:3],
    }, res


def overall_score(results: list[dict]) -> int | None:
    scores = [r["freshness_score"] for r in results if r["verdict"] != "Unverifiable"]
    return round(sum(scores) / len(scores)) if scores else None
