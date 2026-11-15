"""BM25 search over build/kb.json (no embeddings, no vector DB)."""
import json
import re

from rank_bm25 import BM25Okapi

import config

_TOKEN = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")
_kb: list[dict] | None = None
_bm25: BM25Okapi | None = None


def _tok(text: str) -> list[str]:
    return _TOKEN.findall(text.lower())


def load() -> list[dict]:
    global _kb, _bm25
    if _kb is None:
        if not config.KB_JSON.exists():
            raise FileNotFoundError("build/kb.json not found. Run: python ingest.py")
        _kb = json.loads(config.KB_JSON.read_text(encoding="utf-8"))
        _bm25 = BM25Okapi([_tok(f"{c['brief_id']} {c['title']} {c['text']}") for c in _kb])
    return _kb


def search(query: str, k: int = 5) -> list[dict]:
    kb = load()
    scores = _bm25.get_scores(_tok(query))
    top = sorted(range(len(kb)), key=lambda i: scores[i], reverse=True)[:k]
    return [{**kb[i], "score": round(float(scores[i]), 2)} for i in top if scores[i] > 0]


if __name__ == "__main__":
    import sys
    q = " ".join(sys.argv[1:]) or "NPC breach notification 72 hours"
    for r in search(q):
        print(f"{r['score']:6.2f}  {r['brief_id']}  {r['title'][:60]}  (reviewed {r['review_date']})")
        print("        ", r["text"][:160].replace("\n", " "))
