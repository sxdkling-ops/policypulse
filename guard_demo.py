"""Streamlit tabs for the Freshness Guard plugin: a client-side demo and the gateway audit dashboard."""
import json
import time

import altair as alt
import httpx
import pandas as pd
import streamlit as st

import config
from fixer import diff_html

if config.DEMO_MODE:  # keyless demo: a mock model with canned 2024 answers, checks replayed from a recording
    MODELS = {"openai/mock-2024": "Demo model (canned answers from a 2024-era model, no API keys)"}
else:
    MODELS = {
        "groq/openai/gpt-oss-120b": "Groq gpt-oss-120b (open model, knowledge from 2024)",
        "anthropic/claude-haiku-5-5": "Claude Haiku 5.5 (current model)",
    }
PRESETS = {
    "Latest Anthropic model": "What is Anthropic's newest Claude model and what does its API cost per million tokens? "
                              "Answer in 3 sentences.",
    "EU AI Act": "When did the EU AI Act enter into force? Answer in 2 sentences.",
    "Microsoft CEO": "Who is the CEO of Microsoft and since when? Answer in 2 sentences.",
    "Nothing time-sensitive": "Explain in 2 sentences what an API gateway does.",
}
MODES = {
    "audit": "Audit: return the answer at once, check in the background",
    "annotate": "Annotate: wait for the check, attach verdicts",
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


def _claims_table(claims: list[dict]):
    st.dataframe(pd.DataFrame([{"verdict": c.get("verdict"), "score": c.get("freshness_score"),
                                "claim": c.get("claim"), "official sources": c.get("official_sources"),
                                "correction": c.get("suggested_correction", "")} for c in claims]),
                 hide_index=True, width="stretch")


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
                              "earns its keep. All models are served through the same Bifrost gateway.")
    mode = c2.radio("Guard mode (x-freshness-guard header)", list(MODES), format_func=MODES.get, key="guard_mode")
    if st.button("Send through Bifrost", type="primary", key="guard_send"):
        with st.spinner("Calling the model through Bifrost..."):
            try:
                st.session_state["guard_result"] = call_gateway(prompt, mode, model)
                st.session_state.pop("guard_audit", None)
            except httpx.HTTPError as e:
                st.error(f"Could not reach Bifrost at {config.BIFROST_URL}: {e}")
    res = st.session_state.get("guard_result")
    if not res:
        st.caption("Tip: send the same question twice. The second answer comes from the verdict cache.")
        return

    body, headers = res["body"], res["headers"]
    fresh = body.get("freshness") or (body.get("error") or {}).get("freshness") or {}
    status = headers.get("x-freshness-status", "none")
    m = st.columns(5)
    m[0].metric("HTTP status", res["status"])
    m[1].metric("Guard status", status)
    m[2].metric("Freshness score", headers.get("x-freshness-score", "n/a"))
    m[3].metric("Stale claims", headers.get("x-freshness-stale-claims", "n/a"))
    m[4].metric("Round trip", f"{res['seconds']} s")
    st.caption("Headers added by the plugin: " + (", ".join(f"`{k}: {v}`" for k, v in headers.items()) or "none"))

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

    if status == "pending":
        check_id = headers.get("x-freshness-check-id", "")
        st.info("The answer came back without waiting. The check is running in the background and will appear in "
                "the **Gateway audit** tab.")
        if st.button("Get the audit result", key="guard_poll"):
            with st.spinner("Waiting for the background check..."):
                for _ in range(60):
                    r = httpx.get(f"{config.CHECKER_URL}/audit/{check_id}", timeout=10)
                    if r.status_code == 200:
                        st.session_state["guard_audit"] = r.json()
                        break
                    time.sleep(2)
        ev = st.session_state.get("guard_audit")
        if ev and ev.get("check_id") == check_id:
            a = st.columns(4)
            a[0].metric("Audit result", ev.get("action"))
            a[1].metric("Freshness score", ev.get("score") if ev.get("score") is not None else "n/a")
            a[2].metric("Stale claims", ev.get("stale_claims"))
            a[3].metric("Check time", f"{round(ev.get('check_ms', 0) / 1000, 1)} s")
            if ev.get("claims"):
                _claims_table(ev["claims"])
    elif status.startswith("skipped"):
        st.info({"skipped-timeless": "Skipped: nothing in this answer can go stale (no dates, prices, versions or "
                                     "legal terms), so no check was run and no cost was incurred.",
                 "skipped-short": "Skipped: the answer is too short to check."}.get(status, f"Skipped ({status})."))
    if fresh.get("claims"):
        st.markdown("##### Claims the plugin checked")
        _claims_table(fresh["claims"])
    with st.expander("Raw JSON returned by Bifrost"):
        st.json(body)


def render_audit():
    st.markdown("Every answer the plugin checks is reported to an audit webhook. This is what a gateway operator "
                "sees: which models give stale answers, how often, and what was blocked or fixed.")
    c1, c2, _ = st.columns([1, 1, 6])
    c1.button("Refresh", key="audit_refresh")
    if c2.button("Clear", key="audit_clear"):
        try:
            httpx.delete(f"{config.CHECKER_URL}/audit", timeout=10)
        except httpx.HTTPError:
            pass
    try:
        stats = httpx.get(f"{config.CHECKER_URL}/audit/stats", timeout=10).json()
        events = httpx.get(f"{config.CHECKER_URL}/audit", params={"limit": 200}, timeout=10).json()
    except (httpx.HTTPError, json.JSONDecodeError) as e:
        st.error(f"Could not reach the checker API at {config.CHECKER_URL}: {e}")
        return
    if not events:
        st.info("No answers audited yet. Send a few requests from the Freshness Guard tab.")
        return
    ev = pd.DataFrame(events)
    m = st.columns(5)
    m[0].metric("Answers checked", len(ev))
    m[1].metric("With stale claims", int((ev.get("stale_claims", 0) > 0).sum()))
    m[2].metric("Blocked", int((ev["action"] == "blocked").sum()))
    m[3].metric("Fixed", int((ev["action"] == "fixed").sum()))
    m[4].metric("Cache hits", int((ev.get("cache") == "hit").sum()))

    sdf = pd.DataFrame(stats)
    left, right = st.columns([2, 3])
    with left:
        st.markdown("##### Average freshness by model")
        chart = sdf.dropna(subset=["avg_freshness"])
        if len(chart):
            base = alt.Chart(chart).encode(
                y=alt.Y("model:N", sort="x", title=None, axis=alt.Axis(labelLimit=220)),
                x=alt.X("avg_freshness:Q", scale=alt.Scale(domain=[0, 100]), title="Average freshness (0-100)"),
                tooltip=["model", "avg_freshness", "answers_checked", "stale_answers"])
            bars = base.mark_bar(size=26, cornerRadiusEnd=4).encode(color=alt.Color(
                "avg_freshness:Q", legend=None,
                scale=alt.Scale(type="threshold", domain=[40, 70], range=["#cf222e", "#b08800", "#1a7f37"])))
            labels = base.mark_text(align="left", dx=6, fontSize=13).encode(text="avg_freshness:Q")
            st.altair_chart((bars + labels).properties(height=70 + 44 * len(chart)), width="stretch")
            st.caption("Red: below 40 (block threshold). Amber: 40 to 69. Green: 70 and above.")
    with right:
        st.markdown("##### By model")
        st.dataframe(sdf, hide_index=True, width="stretch")
    st.markdown("##### Audit events")
    cols = [c for c in ["time", "model", "mode", "action", "score", "stale_claims", "claims_checked", "cache",
                        "check_ms", "stream", "check_id", "answer_excerpt"] if c in ev]
    st.dataframe(ev[cols], hide_index=True, width="stretch", height=360)
    st.download_button("Export audit log (CSV)", ev[cols].to_csv(index=False), file_name="freshness_audit.csv",
                       key="audit_csv")
