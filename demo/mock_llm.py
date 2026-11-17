"""A tiny OpenAI-compatible model for the keyless demo and CI.

It replays answers recorded from a real 2024-era model (demo/canned.json), so Bifrost and the Freshness Guard
plugin can be exercised end to end without any API keys. Supports normal and streaming chat completions.

Run: uvicorn demo.mock_llm:app --port 9000
"""
import json
import time
import uuid
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

CANNED = json.loads((Path(__file__).parent / "canned.json").read_text(encoding="utf-8"))
app = FastAPI(title="Mock 2024-era model")


def pick_answer(messages: list[dict]) -> str:
    prompt = " ".join(str(m.get("content", "")) for m in messages if m.get("role") == "user").lower()
    for item in CANNED:
        if item["match"] and any(k in prompt for k in item["match"]):
            return item["answer"]
    return next(i["answer"] for i in CANNED if not i["match"])


def usage(answer: str) -> dict:
    out = max(1, len(answer) // 4)
    return {"prompt_tokens": 20, "completion_tokens": out, "total_tokens": 20 + out}


@app.get("/health")
def health():
    return {"ok": True, "answers": len(CANNED)}


@app.get("/v1/models")
@app.get("/models")
def models():
    return {"object": "list", "data": [{"id": "mock-2024", "object": "model", "owned_by": "demo"}]}


@app.post("/v1/chat/completions")
@app.post("/chat/completions")
async def chat(request: Request):
    body = await request.json()
    model = body.get("model", "mock-2024")
    answer = pick_answer(body.get("messages", []))
    cid, created = "chatcmpl-" + uuid.uuid4().hex[:24], int(time.time())
    if not body.get("stream"):
        return JSONResponse({
            "id": cid, "object": "chat.completion", "created": created, "model": model,
            "choices": [{"index": 0, "finish_reason": "stop",
                         "message": {"role": "assistant", "content": answer}}],
            "usage": usage(answer)})

    def chunk(delta: dict, finish=None) -> str:
        return "data: " + json.dumps({"id": cid, "object": "chat.completion.chunk", "created": created,
                                      "model": model,
                                      "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}) + "\n\n"

    def events():
        yield chunk({"role": "assistant", "content": ""})
        words = answer.split(" ")
        for i in range(0, len(words), 4):
            yield chunk({"content": " ".join(words[i:i + 4]) + (" " if i + 4 < len(words) else "")})
            time.sleep(0.02)
        yield chunk({}, "stop")
        yield "data: [DONE]\n\n"

    return StreamingResponse(events(), media_type="text/event-stream")
