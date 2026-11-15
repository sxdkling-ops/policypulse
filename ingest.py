"""Build build/index.json, build/register.json and build/kb.json from the KB PDFs.

Usage:
  python ingest.py            # index + register + chunks for config.KB_FOLDERS
  python ingest.py --index    # only the two index files (fast)
"""
import json
import re
import sys
from datetime import datetime
from pathlib import Path

import pymupdf

import config

ID_RE = re.compile(r"\b((?:PH|AS|IN|TG)-\d{2,3})(?!\d)")
DATE_RE = re.compile(r"(\d{1,2})\s+(January|February|March|April|May|June|July|August|September|October|"
                     r"November|December)\s+(\d{4})")
NOISE_RE = re.compile(r"^(\d+ / \d+|AI GRC Pilipinas .*)$")
CHUNK_CHARS = 3200  # ~800 tokens

# Review frequency (days) by brief family, from the Master Index update schedule.
REVIEW_DAYS = {"PH": 90, "AS": 180, "IN": 365, "TG": 365}
STATUSES = {"IN FORCE", "VOLUNTARY", "PRACTICE GUIDE", "GUIDANCE", "PHASED", "PENDING", "POLICY",
            "SUPERSEDED", "REPEALED", "DRAFT"}
MONTHLY_KEYWORDS = ("NPC Advis", "BSP Memorandum", "Pending", "Supreme Court", "COMELEC", "DICT")


def to_iso(text: str) -> str | None:
    m = DATE_RE.search(text or "")
    if not m:
        return None
    return datetime.strptime(" ".join(m.groups()), "%d %B %Y").date().isoformat()


def clean_lines(text: str) -> list[str]:
    return [ln.strip() for ln in text.splitlines() if ln.strip() and not NOISE_RE.match(ln.strip())]


def pdf_pages(path: Path) -> list[str]:
    with pymupdf.open(path) as doc:
        return [p.get_text() for p in doc]


# ---------- brief headers -> index.json ----------
def parse_brief_header(path: Path, first_page: str) -> dict:
    lines = clean_lines(first_page)
    m = ID_RE.match(path.name)
    brief_id = m.group(1) if m else path.stem
    meta = {"brief_id": brief_id, "file": path.name, "folder": path.parent.name,
            "title": "", "type": "", "status": "", "review_date": None, "source_url": ""}
    # Line 0 = spaced-out kicker ("P H - 0 3 · ..."), line 1 = STATUS, then title lines until the issuer line.
    title, i = [], 0
    for i, ln in enumerate(lines[:8]):
        if ln in STATUSES:
            meta["status"] = ln
            break
    for ln in lines[i + 1:]:
        if meta["status"]:
            if ln.startswith(("Official source", "Last reviewed")) or re.search(r"\d{4}$", ln) and title:
                break
            title.append(ln)
    meta["title"] = " ".join(title).replace("- ", "-")[:200]
    text = "\n".join(lines)
    if (r := re.search(r"Last reviewed:\s*(.+)", text)):
        meta["review_date"] = to_iso(r.group(1))
    if (r := re.search(r"Official source[^:]*:\s*(\S+)", text)):
        meta["source_url"] = r.group(1)
    if (r := re.search(r"\nType\n(.+)", text)):
        meta["type"] = r.group(1)
    days = REVIEW_DAYS.get(brief_id[:2], 365)
    if brief_id.startswith("PH") and any(k in meta["title"] for k in MONTHLY_KEYWORDS):
        days = 30
    meta["review_every_days"] = days
    return meta


def all_brief_files() -> list[Path]:
    root = config.KB_PATH
    return sorted(p for p in root.rglob("*.pdf") if ID_RE.match(p.name))


def build_index(register: list[dict] | None = None) -> list[dict]:
    titles = {r["brief_id"]: r["brief_title"] for r in (register or [])}
    counts: dict[str, int] = {}
    for r in register or []:
        counts[r["brief_id"]] = counts.get(r["brief_id"], 0) + 1
    rows = []
    for p in all_brief_files():
        with pymupdf.open(p) as doc:
            meta = parse_brief_header(p, doc[0].get_text())
        meta["title"] = titles.get(meta["brief_id"], meta["title"])
        meta["open_items"] = counts.get(meta["brief_id"], 0)
        rows.append(meta)
    config.INDEX_JSON.write_text(json.dumps(rows, indent=1, ensure_ascii=False), encoding="utf-8")
    return rows


# ---------- Verification Register -> register.json ----------
def find_register() -> Path | None:
    hits = list(config.KB_PATH.rglob("00_Verification-Register.pdf"))
    return hits[0] if hits else None


def build_register() -> list[dict]:
    path = find_register()
    if not path:
        config.REGISTER_JSON.write_text("[]", encoding="utf-8")
        return []
    lines = clean_lines("\n".join(pdf_pages(path)))
    start = next((i for i, ln in enumerate(lines) if ln.startswith("Open items")), 0)
    items, cur, src, i = [], None, "", start
    head_re = re.compile(r"^((?:PH|AS|IN|TG)-\d{2,3}) . (.+)$")
    while i < len(lines):
        ln = lines[i]
        h = head_re.match(ln)
        if h and not ln.startswith(("Open items",)):
            cur = {"brief_id": h.group(1), "title": h.group(2)}
            # title may wrap onto the next line(s) until "Official source"
            while i + 1 < len(lines) and not lines[i + 1].startswith(("Official source", "#")):
                i += 1
                cur["title"] += " " + lines[i]
            src = ""
        elif ln.startswith("Official source to check:"):
            src = ln.split(":", 1)[1].strip()
            while i + 1 < len(lines) and lines[i + 1] != "#":
                i += 1
                src += lines[i]
        elif cur and ln.isdigit() and i + 1 < len(lines) and lines[i + 1] in ("Listed", "Inline"):
            num, where, text = int(ln), lines[i + 1], []
            i += 2
            while i < len(lines):
                nxt = lines[i]
                if (nxt.isdigit() and i + 1 < len(lines) and lines[i + 1] in ("Listed", "Inline")) \
                        or head_re.match(nxt) or nxt.startswith("Open items") or nxt in ("#", "Where", "Item"):
                    break
                text.append(nxt)
                i += 1
            items.append({"item_id": f"{cur['brief_id']}#{num}", "brief_id": cur["brief_id"],
                          "brief_title": cur["title"].strip(), "where": where, "item": " ".join(text),
                          "official_source": src})
            continue
        i += 1
    config.REGISTER_JSON.write_text(json.dumps(items, indent=1, ensure_ascii=False), encoding="utf-8")
    return items


# ---------- chunks -> kb.json ----------
def chunk_text(text: str) -> list[str]:
    paras, chunks, cur = re.split(r"\n(?=[A-Z])", text), [], ""
    for p in paras:
        if len(cur) + len(p) > CHUNK_CHARS and cur:
            chunks.append(cur.strip())
            cur = cur[-300:]  # small overlap
        cur += "\n" + p
    if cur.strip():
        chunks.append(cur.strip())
    return chunks


def build_kb(index: list[dict]) -> list[dict]:
    meta_by_file = {r["file"]: r for r in index}
    files = []
    for folder in config.KB_FOLDERS:
        d = config.KB_PATH / folder
        files += sorted(d.glob("*.pdf")) if d.exists() else []
    if not files:  # sample KB: flat folder
        files = sorted(p for p in config.KB_PATH.glob("*.pdf") if ID_RE.match(p.name))
    kb = []
    for p in files:
        meta = meta_by_file.get(p.name, {"brief_id": p.stem[:5], "title": p.stem, "review_date": None})
        text = "\n".join("\n".join(clean_lines(pg)) for pg in pdf_pages(p))
        for n, c in enumerate(chunk_text(text)):
            kb.append({"id": f"{meta['brief_id']}:{n}", "brief_id": meta["brief_id"], "title": meta["title"],
                       "folder": p.parent.name, "review_date": meta.get("review_date"), "text": c})
    config.KB_JSON.write_text(json.dumps(kb, ensure_ascii=False), encoding="utf-8")
    return kb


if __name__ == "__main__":
    print(f"KB_PATH = {config.KB_PATH}{'  (sample)' if config.USING_SAMPLE_KB else ''}")
    register = build_register()
    index = build_index(register)
    print(f"index.json: {len(index)} briefs | register.json: {len(register)} open items")
    for r in index[:3] + index[-2:]:
        print("  ", json.dumps(r, ensure_ascii=False)[:260])
    for r in register[:3]:
        print("  ", json.dumps(r, ensure_ascii=False)[:260])
    if "--index" not in sys.argv:
        kb = build_kb(index)
        print(f"kb.json: {len(kb)} chunks from {len({c['brief_id'] for c in kb})} briefs")
