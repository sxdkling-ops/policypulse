"""Rewrite the checked answer with the judge's corrections, and show what changed."""
import difflib
import html

from llm import chat

SYSTEM = """You correct an AI answer. Apply ONLY the listed corrections. Keep every other sentence exactly as written.
Do not add new facts, sources or commentary. Return only the corrected answer text."""


def fix_answer(answer: str, results: list[dict]) -> str | None:
    fixes = [r for r in results if r.get("suggested_correction") and r["verdict"] in ("Outdated", "Possibly stale")]
    if not fixes:
        return None
    lst = "\n".join(f"- Claim: {r['claim']}\n  Correction: {r['suggested_correction']}" for r in fixes)
    res = chat(f"ANSWER:\n{answer}\n\nCORRECTIONS:\n{lst}", system=SYSTEM, max_tokens=4000, purpose="fix")
    return res["text"].strip() or None


def diff_html(old: str, new: str) -> str:
    """Word-level diff: removed text struck through in red, added text in green."""
    a, b = old.split(), new.split()
    out = []
    for op, i1, i2, j1, j2 in difflib.SequenceMatcher(a=a, b=b, autojunk=False).get_opcodes():
        if op == "equal":
            out.append(html.escape(" ".join(a[i1:i2])))
            continue
        if i2 > i1:
            out.append("<del style='background:#ffd7d5;color:#82071e'>" + html.escape(" ".join(a[i1:i2])) + "</del>")
        if j2 > j1:
            out.append("<ins style='background:#d1f4d9;color:#055d20;text-decoration:none'>"
                       + html.escape(" ".join(b[j1:j2])) + "</ins>")
    return " ".join(out)
