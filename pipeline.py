"""Answer -> claims -> Tavily -> judge -> corrected answer. Shared by the app, the API and the smoke test."""
import re
from datetime import date, datetime

from extractor import extract_claims
from fixer import fix_answer
from judge import judge_claim, overall_score
from verifier import verify_claim


def stale_risk(implied_date) -> str | None:
    """Flag claims that assume a date more than a year old, before any search."""
    years = [int(y) for y in re.findall(r"(?<!\d)(19\d{2}|20\d{2})(?!\d)", str(implied_date or ""))]
    if years and max(years) < date.today().year - 1:
        return f"Assumes {max(years)}: over a year old, high stale risk"
    return None


def check_answer(answer: str, progress=None, fix: bool = True) -> dict:
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
            v = {"verdict": "Unverifiable", "freshness_score": 50, "score_parts": [("Check failed", 50)],
                 "evidence": f"Check failed: {e}", "suggested_correction": "", "sources": [], "cached": False}
        results.append({**c, **v, "stale_risk": stale_risk(c.get("implied_date"))})
    fixed = None
    if fix:
        try:
            fixed = fix_answer(answer, results)
        except Exception:
            fixed = None
    return {"answer": answer, "results": results, "overall": overall_score(results), "fixed_answer": fixed,
            "checked_at": datetime.now().isoformat(timespec="seconds")}


def to_markdown(report: dict, title: str = "PolicyPulse freshness report", extra: str = "") -> str:
    lines = [f"# {title}", "", f"Checked: {report['checked_at']}",
             f"Overall freshness: {report['overall'] if report['overall'] is not None else 'n/a'}/100", ""]
    if extra:
        lines += [extra, ""]
    lines += ["## Answer checked", "", "> " + report["answer"].replace("\n", "\n> "), ""]
    if report.get("fixed_answer"):
        lines += ["## Corrected answer", "", "> " + report["fixed_answer"].replace("\n", "\n> "), ""]
    lines += ["## Claims", ""]
    for i, r in enumerate(report["results"], 1):
        lines += [f"### {i}. {r['verdict']} ({r['freshness_score']}/100)", "", f"**Claim:** {r['claim']}",
                  f"**Category:** {r['category']}", f"**Evidence:** {r['evidence']}"]
        if r.get("stale_risk"):
            lines.append(f"**Stale risk:** {r['stale_risk']}")
        if r.get("score_parts"):
            lines.append("**Score:** " + " · ".join(f"{lbl} ({pts:+d})" if j else f"{lbl} ({pts})"
                                                   for j, (lbl, pts) in enumerate(r["score_parts"])))
        if r.get("suggested_correction"):
            lines.append(f"**Suggested correction:** {r['suggested_correction']}")
        for s in r.get("sources", []):
            tag = " (official)" if s.get("official") else ""
            lines.append(f"- [{s['title'] or s['url']}]({s['url']}){tag} {s.get('published_date') or ''}")
        lines.append("")
    return "\n".join(lines)
