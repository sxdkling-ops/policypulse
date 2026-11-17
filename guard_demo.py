"""Streamlit tab that calls Bifrost directly so the Freshness Guard plugin's work is visible."""
import json
import time

import httpx
import pandas as pd
import streamlit as st

import config
from fixer import diff_html

MODELS = {
    "groq/openai/gpt-oss-120b": "Groq gpt-oss-120b (open model, knowledge from 2024)",
    "anthropic/claude-haiku-5-5": "Claude Haiku 5.5 (current model)",
}
PRESETS = {
    "Latest Anthropic model": "What is Anthropic's newest Claude model and what does its API cost per million tokens?",
    "Philippine regulators and AI": "Have Philippine regulators such as the Bangko Sentral ng Pilipinas or the "
                                    "National Privacy Commission issued guidance on AI? Summarise what exists in "
                                    "4 sentences.",
    "EU AI Act status": "What is the current legal status of the EU AI Act, and when do its obligations apply? "
                        "Answer in 4 sentences.",
}
MODES = {
    "annotate": "Annotate: pass the answer through and attach verdicts",
    "fix": "Fix: replace stale answers with the corrected version",
    "block": "Block: refuse answers below the freshness threshold (HTTP 422)",
}


def call_gateway(prompt: str, mode: str, model: str) -> dict:
    t0 = time.perf_counter()
    r = httpx.post(f"{config.BIFROST_URL}/chat/completions",
                   json={"model": model, "messages": [{"role": "user", "content": prompt}], "max_tokens": 2000},
                   headers={"x-freshness-guard": mode}, timeout=240)
    try:
        body = r.json()
    except json.JSONDecodeError:
        body = {"raw": r.text[:2000]}
    return {"status": r.status_code, "headers": {k: v for k, v in r.headers.items() if k.startswith("x-freshness")},
            "body": body, "seconds": round(time.perf_counter() - t0, 1)}


def _answer(body: dict) -> str:
    try:
        return body["choices"][0]["message"]["content"] or ""
    except (KeyError, IndexError, TypeError):
        return ""


def render():
    st.markdown(
        "**Freshness Guard** is a Bifrost plugin. Any app that sends chat completions through Bifrost gets its "
        "answers checked for stale claims, with no change to the app. The request below goes to Bifrost like any "
        "OpenAI-compatible call; the only extra is the optional `x-freshness-guard` header that picks the mode "
        "for this request.")
    c1, c2 = st.columns([3, 2])
    preset = c1.selectbox("Question", list(PRESETS), key="guard_preset")
    prompt = c1.text_area("Prompt sent to the model", PRESETS[preset], height=90, key=f"guard_prompt_{preset}")
    model = c2.selectbox("Model behind Bifrost", list(MODELS), format_func=MODELS.get, key="guard_model",
                         help="Models with an older knowledge cutoff go stale fastest; that is where the guard "
                              "earns its keep. Both are served through the same Bifrost gateway.")
    mode = c2.radio("Guard mode (x-freshness-guard header)", list(MODES), format_func=MODES.get, key="guard_mode")
    if st.button("Send through Bifrost", type="primary", key="guard_send"):
        with st.spinner("Bifrost is calling the model, then the Freshness Guard is checking the answer live..."):
            try:
                st.session_state["guard_result"] = call_gateway(prompt, mode, model)
            except httpx.HTTPError as e:
                st.error(f"Could not reach Bifrost at {config.BIFROST_URL}: {e}")
    res = st.session_state.get("guard_result")
    if not res:
        st.caption("Tip: run the same question in annotate, fix and block mode to compare.")
        return

    body, headers = res["body"], res["headers"]
    fresh = body.get("freshness") or (body.get("error") or {}).get("freshness") or {}
    m = st.columns(4)
    m[0].metric("HTTP status", res["status"])
    m[1].metric("Guard status", headers.get("x-freshness-status", "none"))
    m[2].metric("Freshness score", headers.get("x-freshness-score", "n/a"))
    m[3].metric("Stale claims", headers.get("x-freshness-stale-claims", "n/a"))
    st.caption(f"Round trip {res['seconds']} s (model + live verification). Headers added by the plugin: "
               + ", ".join(f"`{k}: {v}`" for k, v in headers.items()))

    delivered = _answer(body)
    original = fresh.get("original_answer") or delivered
    left, right = st.columns(2)
    with left:
        st.markdown("##### What the model said")
        st.info(original or "(no answer)")
    with right:
        st.markdown("##### What your app receives")
        if res["status"] == 422:
            st.error(body.get("error", {}).get("message", "Blocked by Freshness Guard"))
        elif delivered and delivered != original:
            st.markdown(diff_html(original, delivered), unsafe_allow_html=True)
        else:
            st.success(delivered or "(no answer)")
    if fresh.get("claims"):
        st.markdown("##### Claims the plugin checked")
        st.dataframe(pd.DataFrame([{"verdict": c.get("verdict"), "score": c.get("freshness_score"),
                                    "claim": c.get("claim"), "official sources": c.get("official_sources"),
                                    "correction": c.get("suggested_correction", "")} for c in fresh["claims"]]),
                     hide_index=True, width="stretch")
    with st.expander("Raw JSON returned by Bifrost"):
        st.json(body)
