"""PolicyPulse - stale-answer detector for AI governance & compliance answers."""
import json
from datetime import date, timedelta

import pandas as pd
import streamlit as st

import config
import guard_demo
import llm
from judge import judge_claim
from llm import GatewayError, chat
from pipeline import check_answer, to_markdown
from authority import domain
from fixer import diff_html
from verifier import verify

st.set_page_config(page_title="PolicyPulse", page_icon="🩺", layout="wide")

COLORS = {"Current": "#1a7f37", "Possibly stale": "#b08800", "Outdated": "#cf222e", "Unverifiable": "#6e7781"}
SAMPLES = {
    "AI regulation (has a stale claim)":
        "The EU AI Act is still a proposal and has not been adopted. In the Philippines, the National Privacy "
        "Commission has not issued any guidance on AI systems that process personal data. Companies must notify "
        "the NPC of a personal data breach within 72 hours of discovery.",
    "LLM API pricing":
        "OpenAI's GPT-4 API costs $0.03 per 1K input tokens and $0.06 per 1K output tokens, and GPT-4 has a "
        "maximum context window of 8K tokens. Anthropic's newest model is Claude 2.",
    "Philippine AI & privacy rules (KB topics)":
        "In the Philippines, the Bangko Sentral ng Pilipinas has not issued any guidance on AI governance for banks. "
        "The National Privacy Commission's main AI guidance is NPC Advisory No. 2024-04. A personal data breach "
        "must be reported to the NPC within 72 hours.",
    "Company CEO":
        "Twitter is run by CEO Parag Agrawal. Microsoft's CEO is Satya Nadella, and Google's parent company "
        "Alphabet is led by Sundar Pichai.",
}


# ---------- helpers ----------
def setup_warnings():
    if config.DEMO_MODE:
        st.info("Keyless demo: Bifrost and the Freshness Guard plugin are real; the model is a mock that replays "
                "answers from a 2024-era model, and the checker replays the live checks recorded for them. The "
                "first two tabs work here. The other tabs need the full stack with API keys.")
        return
    if not config.TAVILY_API_KEY:
        st.warning("TAVILY_API_KEY is missing in .env, so live verification is off (cached results still work).")
    if config.USING_SAMPLE_KB:
        st.info("KB_PATH not set or not found: using the sample PDFs in data/sample/.")


def verdict_card(r: dict):
    color = COLORS.get(r["verdict"], "#6e7781")
    with st.container(border=True):
        c1, c2 = st.columns([5, 1])
        c1.markdown(f"**{r['claim']}**  \n`{r.get('category', 'other')}`"
                    + (f" · implied date: {r['implied_date']}" if r.get("implied_date") else ""))
        if r.get("stale_risk"):
            c1.markdown(f"<span style='background:#fff1cc;color:#7a5200;border-radius:6px;padding:2px 8px;"
                        f"font-size:0.85em'>{r['stale_risk']}</span>", unsafe_allow_html=True)
        c2.markdown(f"<div style='background:{color};color:white;border-radius:8px;padding:6px;text-align:center'>"
                    f"<b>{r['verdict']}</b><br>{r['freshness_score']}/100</div>", unsafe_allow_html=True)
        if r.get("evidence"):
            st.markdown(f"**Evidence:** {r['evidence']}")
        if r.get("suggested_correction"):
            st.markdown(f"**Suggested correction:** {r['suggested_correction']}")
        if r.get("score_parts"):
            with st.expander(f"Why {r['freshness_score']}/100?"):
                for j, (label, pts) in enumerate(r["score_parts"]):
                    st.markdown(f"- {label}: **{pts if j == 0 else f'{pts:+d}'}**")
                st.caption("The LLM picks only the verdict. The score is computed from the verdict, whether an "
                           "official source backs it, how many sources agree, and how recent the evidence is.")
        if r.get("sources"):
            n_off = sum(1 for s in r["sources"] if s.get("official"))
            label = f"Sources ({len(r['sources'])}{f', {n_off} official' if n_off else ''})"
            with st.expander(label + (" · cached" if r.get("cached") else "")):
                for s in r["sources"]:
                    tag = " **· official**" if s.get("official") else ""
                    st.markdown(f"- [{s['title'] or s['url']}]({s['url']}){tag} {s.get('published_date') or ''}")


def show_report(report: dict, key: str, extra_md: str = ""):
    if not report["results"]:
        st.success("No time-sensitive claims found.")
        return
    score = report["overall"]
    counts = pd.Series([r["verdict"] for r in report["results"]]).value_counts()
    cols = st.columns(5)
    cols[0].metric("Overall freshness", f"{score}/100" if score is not None else "n/a")
    for col, v in zip(cols[1:], COLORS):
        col.metric(v, int(counts.get(v, 0)))
    if report.get("fixed_answer"):
        with st.container(border=True):
            st.markdown("#### Corrected answer")
            st.caption("The original answer with only the suggested corrections applied. "
                       "Red = removed, green = added.")
            st.markdown(diff_html(report["answer"], report["fixed_answer"]), unsafe_allow_html=True)
    for r in report["results"]:
        verdict_card(r)
    st.download_button("Download Markdown report", to_markdown(report, extra=extra_md),
                       file_name=f"policypulse_report_{date.today()}.md", key=f"dl_{key}")


def run_check(answer: str) -> dict | None:
    bar = st.progress(0.0, text="Extracting time-sensitive claims...")
    try:
        report = check_answer(answer, progress=lambda i, n, c: bar.progress(
            (i + 1) / max(n, 1), text=f"Verifying claim {i + 1}/{n}: {c[:80]}"))
    except GatewayError as e:
        bar.empty()
        st.error(f"LLM gateway error: {e}")
        return None
    except Exception as e:
        bar.empty()
        st.error(f"Check failed: {e}")
        return None
    bar.empty()
    return report


@st.cache_data
def load_json(path_str: str):
    from pathlib import Path
    p = Path(path_str)
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None


# ---------- page ----------
st.title("PolicyPulse Freshness Guard")
st.caption("A Bifrost plugin that checks AI answers for stale claims before they reach your app, plus the "
           "PolicyPulse checker behind it: claims are re-verified live with Tavily, official sources first.")
setup_warnings()

with st.sidebar:
    st.subheader("Gateway")
    if config.DEMO_MODE:
        st.caption(f"Bifrost: {config.BIFROST_URL}  \nModel: openai/mock-2024 (demo)  \nChecker: replay mode")
    else:
        llm.SIMULATE_PRIMARY_FAILURE = st.toggle(
            "Simulate primary provider failure", value=False,
            help="Sends the request to a broken primary model so Bifrost must use the Groq fallback.")
        st.caption("Turn on, then run any check: Bifrost reroutes every LLM call to Groq and the Gateway log "
                   "tab shows it as a fallback.")
        st.caption(f"Bifrost: {config.BIFROST_URL}  \nPrimary: {config.PRIMARY_MODEL}  \n"
                   f"Fallback: {config.FALLBACK_MODEL}")
    _idx = load_json(str(config.INDEX_JSON)) or []
    st.caption(f"Knowledge base: {'sample' if config.USING_SAMPLE_KB else 'full library'} ({len(_idx)} briefs)")

t_guard, t_audit, t_check, t_ask, t_kb, t_reg, t_gw = st.tabs([
    "Freshness Guard (Bifrost plugin)", "Gateway audit", "Check an Answer", "Ask the KB", "KB Freshness Audit",
    "Verification Register", "Gateway log"])

# Freshness Guard: the Bifrost plugin, seen from a client app
with t_guard:
    guard_demo.render()

# Gateway audit: what the plugin reported for every answer it checked
with t_audit:
    guard_demo.render_audit()

# 1. Check an Answer
with t_check:
    pick = st.selectbox("Load a sample answer", ["(paste your own)"] + list(SAMPLES))
    answer = st.text_area("LLM answer to check", value=SAMPLES.get(pick, ""), height=160)
    if st.button("Check freshness", type="primary", disabled=not answer.strip()):
        st.session_state["check_report"] = run_check(answer)
    if st.session_state.get("check_report"):
        show_report(st.session_state["check_report"], "check")

# 2. Ask the KB
with t_ask:
    q = st.text_input("Ask the AI GRC knowledge base", "When must a personal data breach be reported to the NPC?")
    if st.button("Ask", type="primary"):
        try:
            from retriever import search as kb_search
            hits = kb_search(q, k=config.KB_TOP_K)
        except FileNotFoundError as e:
            st.error(str(e))
            hits = None
        if hits is not None:
            if not hits:
                st.warning("Nothing relevant found in the KB.")
            else:
                ctx = "\n\n".join(f"[{h['brief_id']} | reviewed {h['review_date']}] {h['title']}\n{h['text']}"
                                  for h in hits)
                try:
                    res = chat(f"QUESTION: {q}\n\nKB EXCERPTS:\n{ctx}", purpose="kb_answer", max_tokens=700,
                               system="Answer using ONLY the KB excerpts. Cite brief IDs in brackets like [PH-04] "
                                      "and mention each cited brief's review date once. If the excerpts do not "
                                      "answer the question, say so. Be concise (max 150 words).")
                    st.session_state["kb"] = {"q": q, "answer": res["text"], "hits": hits}
                    with st.spinner("Running the stale check on this answer..."):
                        st.session_state["kb"]["report"] = run_check(res["text"])
                except GatewayError as e:
                    st.error(f"LLM gateway error: {e}")
    kb = st.session_state.get("kb")
    if kb:
        st.markdown("#### Grounded answer")
        st.markdown(kb["answer"])
        with st.expander("Retrieved chunks"):
            for h in kb["hits"]:
                st.markdown(f"**{h['brief_id']}** {h['title']} · reviewed {h['review_date']} · score {h['score']}")
                st.caption(h["text"][:500] + "...")
        if kb.get("report"):
            st.markdown("#### Freshness check of the answer")
            cites = ", ".join(sorted({h["brief_id"] for h in kb["hits"]}))
            show_report(kb["report"], "kb", extra_md=f"Question: {kb['q']}  \nKB briefs retrieved: {cites}")

# 3. KB Freshness Audit
with t_kb:
    index = load_json(str(config.INDEX_JSON))
    if not index:
        st.error("build/index.json not found. Run: python ingest.py")
    else:
        df = pd.DataFrame(index)
        df["review_date"] = pd.to_datetime(df["review_date"])
        df["next_review"] = df["review_date"] + pd.to_timedelta(df["review_every_days"], unit="D")
        as_of = st.date_input("Audit as of", value=date.today(), min_value=date.today() - timedelta(days=30))
        df["days_overdue"] = (pd.Timestamp(as_of) - df["next_review"]).dt.days.clip(lower=0)
        df["overdue"] = df["days_overdue"] > 0
        c = st.columns(4)
        c[0].metric("Briefs", len(df))
        c[1].metric("Overdue for review", int(df["overdue"].sum()))
        c[2].metric("Open verification items", int(df.get("open_items", pd.Series([0])).sum()))
        c[3].metric("Pending / phased", int(df["status"].isin(["PENDING", "PHASED"]).sum()))
        fams = st.multiselect("Family", ["PH", "AS", "IN", "TG"], default=["PH", "AS", "IN", "TG"])
        only = st.checkbox("Show overdue only")
        view = df[df["brief_id"].str[:2].isin(fams)]
        if only:
            view = view[view["overdue"]]
        cols = ["brief_id", "title", "status", "review_date", "review_every_days", "next_review", "days_overdue",
                "open_items"]
        st.dataframe(
            view[cols].style.apply(lambda r: ["background-color: #ffd7d5" if r["days_overdue"] > 0 else ""] * len(r),
                                   axis=1).format({"review_date": "{:%Y-%m-%d}", "next_review": "{:%Y-%m-%d}"}),
            width="stretch", hide_index=True, height=520)
        st.caption("Review cycle per the Master Index schedule: PH AI-specific guidance monthly, PH laws quarterly, "
                   "Asia twice a year, international and topic guides yearly. Move 'Audit as of' forward to "
                   "see what falls due.")

# 4. Verification Register
with t_reg:
    reg = load_json(str(config.REGISTER_JSON))
    if not reg:
        st.error("build/register.json not found or empty. Run: python ingest.py")
    else:
        rdf = pd.DataFrame(reg)
        c1, c2 = st.columns([1, 3])
        brief = c1.selectbox("Brief", sorted(rdf["brief_id"].unique()), index=0)
        sub = rdf[rdf["brief_id"] == brief]
        c2.markdown(f"**{sub.iloc[0]['brief_title']}**  \nOfficial source: {sub.iloc[0]['official_source']}")
        chosen = st.multiselect("Open items to verify (max 5 per run)", sub["item_id"].tolist(),
                                format_func=lambda i: f"{i}: {sub.set_index('item_id').loc[i, 'item'][:110]}",
                                max_selections=5)
        st.dataframe(sub[["item_id", "where", "item"]], hide_index=True, width="stretch")
        if st.button("Verify selected with Tavily + judge", type="primary", disabled=not chosen):
            out = []
            for iid in chosen:
                row = sub.set_index("item_id").loc[iid]
                claim = {"claim": f"{row['brief_title']}: {row['item']}", "implied_date": "November 2026"}
                with st.spinner(f"Verifying {iid}..."):
                    try:
                        where = "Philippines " if iid.startswith("PH") else ""
                        off = [domain(row["official_source"])] if row["official_source"] else []
                        sources, cached = verify(f"{where}{row['brief_title'][:120]} {row['item'][:200]}",
                                                 extra_domains=off)
                        v, _ = judge_claim(claim, sources)
                    except Exception as e:
                        v = {"verdict": "Unverifiable", "freshness_score": 50, "evidence": f"Check failed: {e}",
                             "suggested_correction": "", "sources": [], "score_parts": [("Check failed", 50)]}
                        cached = False
                out.append({"item_id": iid, "claim": row["item"], "category": "regulation", **v, "cached": cached})
            st.session_state["reg_results"] = out
        res = st.session_state.get("reg_results")
        if res:
            for r in res:
                st.markdown(f"**{r['item_id']}**")
                verdict_card(r)
            csv = pd.DataFrame([{**{k: r[k] for k in ("item_id", "claim", "verdict", "freshness_score", "evidence",
                                                      "suggested_correction")},
                                 "sources": " | ".join(s["url"] for s in r["sources"]),
                                 "official_sources": sum(1 for s in r["sources"] if s.get("official"))}
                                for r in res])
            st.download_button("Export CSV", csv.to_csv(index=False), file_name="register_verification.csv")

# 5. Gateway
with t_gw:
    st.markdown("Every LLM call goes through **Bifrost** (OpenAI-compatible gateway). The request names the "
                f"primary model `{config.PRIMARY_MODEL}` and lists `{config.FALLBACK_MODEL}` in Bifrost's "
                "`fallbacks` field, so Bifrost retries on Groq if the primary fails.")
    c1, c2 = st.columns(2)
    if c1.button("Send a test call"):
        try:
            r = chat("Reply with one word: pong", purpose="gateway_test", max_tokens=5)
            st.success(f"{r['model']} answered in {r['latency_ms']} ms"
                       + (" (FALLBACK used)" if r["fallback_used"] else ""))
        except GatewayError as e:
            st.error(str(e))
    if c2.button("Clear log"):
        config.GATEWAY_LOG.unlink(missing_ok=True)
    rows = llm.read_log()
    if not rows:
        st.info("No calls logged yet.")
    else:
        g = pd.DataFrame(rows[::-1])
        ok = g[g["ok"]] if "ok" in g else g
        m = st.columns(4)
        m[0].metric("Calls", len(g))
        m[1].metric("Fallback used", int(ok.get("fallback_used", pd.Series(dtype=bool)).fillna(False).sum()))
        m[2].metric("Avg latency (ms)", int(ok["latency_ms"].mean()) if len(ok) else 0)
        m[3].metric("Tokens", int(ok.get("tokens_in", 0).fillna(0).sum() + ok.get("tokens_out", 0).fillna(0).sum())
                    if len(ok) else 0)
        show = [c for c in ["time", "purpose", "requested_model", "model", "provider", "latency_ms", "tokens_in",
                            "tokens_out", "fallback_used", "simulated_failure", "ok", "error"] if c in g]
        st.dataframe(g[show], hide_index=True, width="stretch")
