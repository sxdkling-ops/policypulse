"""Single helper for every LLM call. All calls go through the Bifrost gateway."""
import json
import re
import time
from datetime import datetime, timezone

from openai import OpenAI

import config

# Set True (e.g. from the Gateway tab) to make the primary model fail on purpose.
SIMULATE_PRIMARY_FAILURE = False
_BROKEN_PRIMARY = "anthropic/simulated-outage-model"


class GatewayError(RuntimeError):
    pass


def _client() -> OpenAI:
    # Bifrost holds the provider keys; the SDK just needs a placeholder.
    return OpenAI(base_url=config.BIFROST_URL, api_key="bifrost", timeout=90, max_retries=0)


def _log(entry: dict) -> None:
    with config.GATEWAY_LOG.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry) + "\n")


def chat(messages, system: str | None = None, max_tokens: int = 1200, temperature: float = 0,
         purpose: str = "") -> dict:
    """Call Bifrost with a primary model and a Groq fallback.

    Returns {text, model, provider, latency_ms, tokens_in, tokens_out, fallback_used}.
    """
    if isinstance(messages, str):
        messages = [{"role": "user", "content": messages}]
    if system:
        messages = [{"role": "system", "content": system}] + messages

    requested = _BROKEN_PRIMARY if SIMULATE_PRIMARY_FAILURE else config.PRIMARY_MODEL
    entry = {"time": datetime.now(timezone.utc).isoformat(timespec="seconds"), "purpose": purpose,
             "requested_model": requested, "simulated_failure": SIMULATE_PRIMARY_FAILURE}
    t0 = time.perf_counter()
    try:
        # Bifrost-native fallback: list of "provider/model" in the request body.
        resp = _client().chat.completions.create(
            model=requested, messages=messages, max_tokens=max_tokens, temperature=temperature,
            extra_body={"fallbacks": [config.FALLBACK_MODEL]},
        )
    except Exception as e:  # gateway down, or both models failed
        entry.update(ok=False, error=str(e)[:300], latency_ms=round((time.perf_counter() - t0) * 1000))
        _log(entry)
        msg = str(e)
        if "Connection" in msg or "connect" in msg:
            msg = f"Cannot reach Bifrost at {config.BIFROST_URL}. Is it running? (npx -y @maximhq/bifrost)"
        raise GatewayError(msg) from e

    latency = round((time.perf_counter() - t0) * 1000)
    served = resp.model or requested
    extra = getattr(resp, "model_extra", None) or {}
    bf = extra.get("extra_fields") or {}
    route = bf.get("routing_info") or {}
    provider = route.get("provider") or bf.get("provider") or ""
    served_full = f"{provider}/{route.get('model') or served}" if provider else served
    fallback_used = bool(route.get("is_fallback")) or config.FALLBACK_MODEL.split("/", 1)[-1] in served_full
    usage = resp.usage
    out = {
        "text": resp.choices[0].message.content or "",
        "model": served_full,
        "provider": provider,
        "latency_ms": latency,
        "tokens_in": getattr(usage, "prompt_tokens", 0) if usage else 0,
        "tokens_out": getattr(usage, "completion_tokens", 0) if usage else 0,
        "fallback_used": bool(fallback_used),
    }
    entry.update(ok=True, **{k: v for k, v in out.items() if k != "text"})
    _log(entry)
    return out


def parse_json(text: str):
    """Pull the first JSON object/array out of a model reply."""
    text = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.M).strip()
    pairs = sorted((("[", "]"), ("{", "}")), key=lambda p: text.find(p[0]) if p[0] in text else len(text))
    for opener, closer in pairs:
        s, e = text.find(opener), text.rfind(closer)
        if s != -1 and e > s:
            try:
                return json.loads(text[s:e + 1])
            except json.JSONDecodeError:
                continue
    # Truncated reply (hit max_tokens): keep every complete {...} object we can decode.
    objs, dec, i = [], json.JSONDecoder(), text.find("{")
    while i != -1:
        try:
            obj, end = dec.raw_decode(text, i)
            objs.append(obj)
            i = text.find("{", end)
        except json.JSONDecodeError:
            break
    if objs:
        return objs if text.lstrip().startswith("[") or len(objs) > 1 else objs[0]
    raise ValueError(f"Model did not return JSON: {text[:200]}")


def read_log() -> list[dict]:
    if not config.GATEWAY_LOG.exists():
        return []
    rows = []
    for line in config.GATEWAY_LOG.read_text(encoding="utf-8").splitlines():
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            pass
    return rows
