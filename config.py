"""Shared settings: env vars and paths."""
import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent
# Repo .env first, then a parent-folder .env (keeps keys outside the repo if you prefer).
load_dotenv(ROOT / ".env")
load_dotenv(ROOT.parent / ".env")

BIFROST_URL = os.getenv("BIFROST_URL", "http://localhost:8080/v1").rstrip("/")
if not BIFROST_URL.endswith("/v1"):
    BIFROST_URL += "/v1"
TAVILY_API_KEY = os.getenv("TAVILY_API_KEY") or os.getenv("TRAVILY") or ""

# Checker API (audit log) and demo switches used by the Streamlit app.
CHECKER_URL = os.getenv("CHECKER_URL", "http://localhost:8000").rstrip("/")
DEMO_MODE = os.getenv("DEMO_MODE", "").strip() in ("1", "true", "yes")

PRIMARY_MODEL = os.getenv("PRIMARY_MODEL", "anthropic/claude-haiku-5-5")
FALLBACK_MODEL = os.getenv("FALLBACK_MODEL", "groq/openai/gpt-oss-120b")

_kb = os.getenv("KB_PATH", "").strip().strip('"')
KB_PATH = Path(_kb) if _kb and Path(_kb).exists() else ROOT / "data" / "sample"
USING_SAMPLE_KB = KB_PATH == ROOT / "data" / "sample"

# Folders (relative to KB_PATH) to ingest. Edit or set KB_FOLDERS="a;b;c".
KB_FOLDERS = [f for f in os.getenv(
    "KB_FOLDERS", "Laws-and-Frameworks/4-Topic-Guides;Laws-and-Frameworks/1-Philippines").split(";") if f]

BUILD = ROOT / "build"
CACHE = ROOT / "cache"
LOGS = ROOT / "logs"
for d in (BUILD, CACHE, LOGS):
    d.mkdir(exist_ok=True)

KB_JSON = BUILD / "kb.json"
INDEX_JSON = BUILD / "index.json"
REGISTER_JSON = BUILD / "register.json"
TAVILY_CACHE = CACHE / "tavily_cache.json"
GATEWAY_LOG = LOGS / "gateway_log.jsonl"
MAX_CLAIMS = 8
# Chunks retrieved per KB question: enough to cover the main brief plus related ones, small enough
# to keep the grounded answer focused and the prompt short.
KB_TOP_K = 5
