"""Record the keyless demo: real answers from a 2024-era model and the real live checks of those answers.

Needs the full stack running with API keys (docker compose up). It writes:
  demo/canned.json   the model's answers, served later by demo/mock_llm.py
  demo/replay.json   the checker's results for those answers, served later in replay mode

Usage: python demo/record_replay.py [bifrost_url] [checker_url]
"""
import hashlib
import json
import sys
import urllib.request
from pathlib import Path

HERE = Path(__file__).parent
BIFROST = (sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8080").rstrip("/")
CHECKER = (sys.argv[2] if len(sys.argv) > 2 else "http://localhost:8000").rstrip("/")
MODEL = "groq/openai/gpt-oss-120b"
PROMPTS = [
    {"match": ["anthropic", "claude"],
     "prompt": "What is Anthropic's newest Claude model and what does its API cost per million tokens? "
               "Answer in 3 sentences."},
    {"match": ["eu ai act"], "prompt": "When did the EU AI Act enter into force? Answer in 2 sentences."},
    {"match": ["microsoft"], "prompt": "Who is the CEO of Microsoft and since when? Answer in 2 sentences."},
    {"match": [], "prompt": "Explain in 2 sentences what an API gateway does."},
]


def post(url: str, payload: dict, headers: dict | None = None) -> dict:
    req = urllib.request.Request(url, data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json", **(headers or {})})
    with urllib.request.urlopen(req, timeout=300) as r:
        return json.loads(r.read().decode("utf-8"))


canned, replay = [], {}
for p in PROMPTS:
    out = post(f"{BIFROST}/v1/chat/completions",
               {"model": MODEL, "max_tokens": 1500, "messages": [{"role": "user", "content": p["prompt"]}]},
               {"x-freshness-guard": "off"})
    answer = out["choices"][0]["message"]["content"].strip()
    canned.append({"match": p["match"], "prompt": p["prompt"], "answer": answer, "recorded_from": MODEL})
    result = post(f"{CHECKER}/check", {"answer": answer, "fix": True})
    result.pop("replayed", None)
    replay[hashlib.sha256(answer.encode("utf-8")).hexdigest()] = result
    print(f"- {p['prompt'][:50]}... score={result['overall_freshness']} stale={result['has_stale_claims']} "
          f"claims={len(result['claims'])}")

(HERE / "canned.json").write_text(json.dumps(canned, indent=1, ensure_ascii=False), encoding="utf-8")
(HERE / "replay.json").write_text(json.dumps(replay, indent=1, ensure_ascii=False), encoding="utf-8")
print("wrote demo/canned.json and demo/replay.json")
