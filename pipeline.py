"""Answer -> claims -> Tavily -> judge. Shared by the app and the smoke test."""
from datetime import datetime

from extractor import extract_claims
from judge import judge_claim, overall_score
from verifier import verify_claim


def check_answer(answer: str, progress=None) -> dict:
    claims, _ = extract_claims(answer)
    results = []
    for i, c in enumerate(claims):
        if progress:
            progress(i, len(claims), c["claim"])
        try:
            sources, cached = verify_claim(c)
            v, _ = judge_claim(c, sources)
            v["cached"] = cached
        except Exception as e:  # keep going on a single failed claim
            v = {"verdict": "Unverifiable", "freshness_score": 50, "evidence": f"Check failed: {e}",
                 "suggested_correction": "", "sources": [], "cached": False}
        results.append({**c, **v})
    return {"answer": answer, "results": results, "overall": overall_score(results),
            "checked_at": datetime.now().isoformat(timespec="seconds")}


def to_markdown(report: dict, title: str = "PolicyPulse freshness report", extra: str = "") -> str:
    lines = [f"# {title}", "", f"Checked: {report['checked_at']}",
             f"Overall freshness: {report['overall'] if report['overall'] is not None else 'n/a'}/100", ""]
    if extra:
        lines += [extra, ""]
    lines += ["## Answer checked", "", "> " + report["answer"].replace("\n", "\n> "), "", "## Claims", ""]
    for i, r in enumerate(report["results"], 1):
        lines += [f"### {i}. {r['verdict']} ({r['freshness_score']}/100)", "", f"**Claim:** {r['claim']}",
                  f"**Category:** {r['category']}", f"**Evidence:** {r['evidence']}"]
        if r.get("suggested_correction"):
            lines.append(f"**Suggested correction:** {r['suggested_correction']}")
        for s in r.get("sources", []):
            lines.append(f"- [{s['title'] or s['url']}]({s['url']}) {s.get('published_date') or ''}")
        lines.append("")
    return "\n".join(lines)
