"""Source authority and recency: which sources are official, and an explainable freshness score."""
import json
import re
from datetime import date, datetime
from email.utils import parsedate_to_datetime
from functools import lru_cache
from urllib.parse import urlparse

import config

# Keyword (lowercase) in a claim -> official domains to search first.
OFFICIAL_BY_KEYWORD = {
    "national privacy commission": ["privacy.gov.ph"], "npc": ["privacy.gov.ph"],
    "data privacy act": ["privacy.gov.ph"],
    "bangko sentral": ["bsp.gov.ph"], "bsp": ["bsp.gov.ph"],
    "dict": ["dict.gov.ph"], "comelec": ["comelec.gov.ph"],
    "republic act": ["officialgazette.gov.ph", "lawphil.net"],
    "philippine": ["officialgazette.gov.ph"], "philippines": ["officialgazette.gov.ph"],
    "eu ai act": ["eur-lex.europa.eu", "digital-strategy.ec.europa.eu"],
    "artificial intelligence act": ["eur-lex.europa.eu", "digital-strategy.ec.europa.eu"],
    "gdpr": ["eur-lex.europa.eu", "edpb.europa.eu"], "european commission": ["ec.europa.eu"],
    "nist": ["nist.gov"], "iso/iec": ["iso.org"], "iso 4": ["iso.org"], "oecd": ["oecd.ai", "oecd.org"],
    "unesco": ["unesco.org"], "pdpc": ["pdpc.gov.sg"], "singapore": ["pdpc.gov.sg", "imda.gov.sg"],
    "monetary authority of singapore": ["mas.gov.sg"],
    "openai": ["openai.com"], "gpt-": ["openai.com"], "anthropic": ["anthropic.com"], "claude": ["anthropic.com"],
    "gemini": ["ai.google.dev"], "microsoft": ["microsoft.com"], "alphabet": ["abc.xyz"],
}
OFFICIAL_SUFFIXES = (".gov", ".gov.ph", ".gov.sg", ".gov.uk", ".europa.eu", ".go.jp", ".gc.ca", ".gov.au",
                     ".int", ".gov.in", ".go.kr")


def domain(url: str) -> str:
    d = urlparse(url if "//" in url else "https://" + url).netloc.lower()
    return d[4:] if d.startswith("www.") else d


@lru_cache(maxsize=1)
def _kb_official_domains() -> frozenset:
    """Domains of every brief's official source in the KB index."""
    try:
        rows = json.loads(config.INDEX_JSON.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return frozenset()
    return frozenset(domain(r["source_url"]) for r in rows if r.get("source_url"))


def official_domains_for(text: str) -> list[str]:
    t = f" {text.lower()} "
    out = []
    for kw, doms in OFFICIAL_BY_KEYWORD.items():
        if re.search(rf"(?<![a-z]){re.escape(kw)}(?![a-z])", t):
            out += [d for d in doms if d not in out]
    return out[:5]


def is_official(url: str) -> bool:
    d = domain(url)
    vendor = {x for doms in OFFICIAL_BY_KEYWORD.values() for x in doms}
    return (d.endswith(OFFICIAL_SUFFIXES) or any(d == v or d.endswith("." + v) for v in vendor)
            or d in _kb_official_domains())


def is_recent(src: dict, months: int = 12) -> bool:
    """Published within ~12 months, from the date field or a year in the URL/title."""
    today = date.today()
    raw = (src.get("published_date") or "").strip()
    if raw:
        d = None
        try:
            d = datetime.fromisoformat(raw.replace("Z", "+00:00")).date()
        except ValueError:
            try:
                d = parsedate_to_datetime(raw).date()
            except (TypeError, ValueError):
                pass
        if d:
            return (today - d).days <= months * 31
    years = [int(y) for y in re.findall(r"(?<!\d)(20\d{2})(?!\d)", f"{src.get('url', '')} {src.get('title', '')}")
             if int(y) <= today.year]
    return bool(years) and max(years) >= today.year - 1


BASE = {"Current": 70, "Possibly stale": 50, "Outdated": 30}


def freshness_score(verdict: str, supporting: list[dict]) -> tuple[int, list[tuple[str, int]]]:
    """Explainable score. Evidence strength pushes the score away from the middle in the verdict's direction."""
    if verdict not in BASE:
        return 50, [("No source settles the claim either way", 50)]
    sign = 1 if verdict == "Current" else -1
    parts = [(f"Verdict: {verdict}", BASE[verdict])]
    if any(s.get("official") for s in supporting):
        parts.append(("Backed by an official source", sign * 10))
    extra = min(max(len(supporting) - 1, 0), 2)
    if extra:
        parts.append((f"{extra + 1} sources agree", sign * 5 * extra))
    if any(is_recent(s) for s in supporting):
        parts.append(("Evidence from the last 12 months", sign * 10))
    return max(0, min(100, sum(p for _, p in parts))), parts
