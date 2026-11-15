"""Extract time-sensitive claims from an LLM answer."""
from datetime import date

import config
from llm import chat, parse_json

SYSTEM = """You extract time-sensitive factual claims from text so they can be re-checked against live sources.
A claim is time-sensitive if it could become false over time: legal/regulatory status (proposed, adopted, in force,
deadlines, fines), prices, API or model versions, company facts (CEO, ownership, products), statistics, dates.
Skip opinions, definitions and timeless facts. Do not add dates or "as of" phrases that are not in the text;
put any date the text assumes in implied_date instead.
Return ONLY a JSON array (max {n} items). Each item:
{{"claim": "<one self-contained sentence, rewritten so it makes sense alone>",
  "category": "regulation|pricing|api_version|company|other",
  "entities": ["<named laws, orgs, products>"],
  "implied_date": "<date/year the claim implies or assumes, or null>"}}
If there are no time-sensitive claims, return []."""


def extract_claims(answer: str) -> tuple[list[dict], dict]:
    """Returns (claims, llm_meta)."""
    res = chat(f"Today is {date.today().isoformat()}.\n\nTEXT:\n{answer}",
               system=SYSTEM.format(n=config.MAX_CLAIMS), max_tokens=3000, purpose="extract")
    claims = parse_json(res["text"])
    if isinstance(claims, dict):
        claims = claims.get("claims", [claims])
    out = []
    for c in claims[: config.MAX_CLAIMS]:
        if isinstance(c, dict) and c.get("claim"):
            c.setdefault("category", "other")
            c.setdefault("entities", [])
            c.setdefault("implied_date", None)
            out.append(c)
    return out, res
