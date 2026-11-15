"""Live verification with Tavily: official sources first, then the open web. Cached on disk to save credits."""
import json
from datetime import date

import config
from authority import domain, is_official, official_domains_for

_cache: dict | None = None
MAX_SOURCES = 7


def _load() -> dict:
    global _cache
    if _cache is None:
        try:
            _cache = json.loads(config.TAVILY_CACHE.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            _cache = {}
    return _cache


def _save() -> None:
    config.TAVILY_CACHE.write_text(json.dumps(_cache, indent=1, ensure_ascii=False), encoding="utf-8")


def search(query: str, include_domains: list[str] | None = None) -> tuple[list[dict], bool]:
    """One Tavily basic search (1 credit). Returns (sources, from_cache)."""
    query = query.strip()[:380]
    key = query + (f" |site:{','.join(include_domains)}" if include_domains else "")
    cache = _load()
    if key in cache:
        return cache[key], True
    if not config.TAVILY_API_KEY:
        raise RuntimeError("TAVILY_API_KEY is missing in .env (get one at app.tavily.com).")
    from tavily import TavilyClient

    kwargs = {"include_domains": include_domains} if include_domains else {}
    resp = TavilyClient(api_key=config.TAVILY_API_KEY).search(
        query=query, search_depth="basic", max_results=5, **kwargs)
    sources = [{"url": r.get("url", ""), "title": r.get("title", ""),
                "snippet": (r.get("content") or "")[:600],
                "published_date": r.get("published_date")} for r in resp.get("results", [])]
    cache[key] = sources
    _save()
    return sources, False


def verify(query: str, hint_text: str = "", extra_domains: list[str] | None = None) -> tuple[list[dict], bool]:
    """Search the official domains for this claim first, then the open web. Official sources are listed first."""
    doms = list(dict.fromkeys((extra_domains or []) + official_domains_for(f"{query} {hint_text}")))[:5]
    results, all_cached = [], True
    if doms:
        found, cached = search(query, include_domains=doms)
        results += found
        all_cached &= cached
    found, cached = search(query)
    results += found
    all_cached &= cached
    seen, out = set(), []
    for s in results:
        if s["url"] and s["url"] not in seen:
            seen.add(s["url"])
            out.append({**s, "official": is_official(s["url"])})
    out.sort(key=lambda s: not s["official"])  # stable: official first
    return out[:MAX_SOURCES], all_cached


def verify_claim(claim: dict, extra_domains: list[str] | None = None) -> tuple[list[dict], bool]:
    hint = " ".join(claim.get("entities") or [])
    return verify(f"{claim['claim']} current status {date.today().year}", hint, extra_domains)


def domain_of(url: str) -> str:
    return domain(url) if url else ""
