"""Live verification of a claim with Tavily search, cached on disk to save credits."""
import json

import config

_cache: dict | None = None


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


def search(query: str) -> tuple[list[dict], bool]:
    """Returns (sources, from_cache). Each source: url, title, snippet, published_date."""
    query = query.strip()[:380]
    cache = _load()
    if query in cache:
        return cache[query], True
    if not config.TAVILY_API_KEY:
        raise RuntimeError("TAVILY_API_KEY is missing in .env (get one at app.tavily.com).")
    from tavily import TavilyClient

    resp = TavilyClient(api_key=config.TAVILY_API_KEY).search(
        query=query, search_depth="basic", max_results=5)
    sources = [{"url": r.get("url", ""), "title": r.get("title", ""),
                "snippet": (r.get("content") or "")[:600],
                "published_date": r.get("published_date")} for r in resp.get("results", [])]
    cache[query] = sources
    _save()
    return sources, False


def verify_claim(claim: dict) -> tuple[list[dict], bool]:
    from datetime import date
    return search(f"{claim['claim']} current status {date.today().year}")
