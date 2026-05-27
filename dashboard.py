from __future__ import annotations

import os
from datetime import date, datetime, timedelta

import streamlit as st
from supabase import create_client, Client

st.set_page_config(
    page_title="Trading Agent C",
    page_icon="C",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ── Connection ─────────────────────────────────────────────────────────────────

@st.cache_resource
def get_client() -> Client:
    url = os.environ.get("SUPABASE_URL") or st.secrets.get("SUPABASE_URL", "")
    key = os.environ.get("SUPABASE_KEY") or st.secrets.get("SUPABASE_KEY", "")
    if not url or not key:
        st.error("Set SUPABASE_URL and SUPABASE_KEY in your environment or .streamlit/secrets.toml")
        st.stop()
    return create_client(url, key)


def q(table: str, cols: str = "*", filters: dict | None = None,
      order: str | None = None, limit: int | None = None) -> list[dict]:
    try:
        db = get_client()
        req = db.table(table).select(cols)
        for k, v in (filters or {}).items():
            req = req.eq(k, v)
        if order:
            desc = order.startswith("-")
            req = req.order(order.lstrip("-"), desc=desc)
        if limit:
            req = req.limit(limit)
        return req.execute().data or []
    except Exception as e:
        st.warning(f"{table}: {e}")
        return []


# ── Helpers ────────────────────────────────────────────────────────────────────

def pnl_color(val: float | None) -> str:
    if val is None:
        return ""
    return "color:#3fb950" if val >= 0 else "color:#f85149"


def fmt_pnl(val: float | None) -> str:
    if val is None:
        return "—"
    sign = "+" if val >= 0 else ""
    return f"{sign}${val:,.2f}"


def fmt_ts(ts: str | None) -> str:
    if not ts:
        return "—"
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        return dt.strftime("%m/%d %H:%M")
    except Exception:
        return ts[:16]


def badge(text: str, color: str = "#388bfd") -> str:
    return f'<span style="background:{color};color:#fff;padding:2px 8px;border-radius:4px;font-size:0.75rem">{text}</span>'


def tier_color(tier: int) -> str:
    return {0: "#3fb950", 1: "#d29922", 2: "#f85149",
            3: "#f85149", 4: "#f85149", 5: "#f85149", 6: "#f85149"}.get(tier, "#888")


# ── Sidebar ────────────────────────────────────────────────────────────────────

with st.sidebar:
    st.markdown("## Trading Agent C")
    st.markdown("---")
    page = st.radio(
        "Navigate",
        ["Overview", "Positions", "Observability", "Sessions", "Parameters", "Goals & Learnings"],
        label_visibility="collapsed",
    )
    st.markdown("---")
    if st.button("Refresh", use_container_width=True):
        st.cache_data.clear()
        st.rerun()
    st.caption(f"Updated {datetime.now().strftime('%H:%M:%S')}")


# ── KPI bar (shown on all pages) ───────────────────────────────────────────────

today = date.today().isoformat()

perf_rows  = q("c_daily_performance", order="-date", limit=1)
open_pos   = q("c_positions", filters={"status": "open"})
protection = q("c_protection_events", order="-created_at", limit=1)
config_row = q("c_agent_config", filters={"config_key": "phase"})

perf       = perf_rows[0] if perf_rows else {}
tier       = protection[0]["tier"] if protection else 0
phase_raw  = config_row[0]["config_value"] if config_row else '"simulation"'
phase      = phase_raw.strip('"')

today_pnl  = perf.get("realized_pnl", 0.0) if perf.get("date") == today else 0.0
win_rate   = perf.get("win_rate", 0.0) if perf.get("date") == today else None
n_open     = len(open_pos)

k1, k2, k3, k4, k5 = st.columns(5)
k1.metric("Today P&L", fmt_pnl(today_pnl), delta_color="normal")
k2.metric("Open Positions", n_open)
k3.metric("Win Rate", f"{win_rate:.0%}" if win_rate is not None else "—")
k4.metric("Protection Tier", tier, delta_color="inverse")
k5.metric("Phase", phase.upper())

st.markdown("---")


# ══════════════════════════════════════════════════════════════════════════════
# PAGE: OVERVIEW
# ══════════════════════════════════════════════════════════════════════════════

if page == "Overview":
    st.header("Overview")

    col_l, col_r = st.columns(2)

    with col_l:
        st.subheader("Open Positions")
        if not open_pos:
            st.info("No open positions")
        else:
            for p in open_pos:
                pnl = p.get("realized_pnl")
                with st.container(border=True):
                    c1, c2, c3 = st.columns([2, 2, 1])
                    c1.markdown(f"**{p['ticker']}**  `{p.get('entry_context','—')}`")
                    c2.markdown(f"Entry **${p['entry_price']:.2f}** → Target **${p['target_price']:.2f}**")
                    c3.markdown(f"Stop **${p['stop_loss']:.2f}**")
                    c1.caption(f"{p['shares']} shares · ${p['position_size']:,.0f}")
                    c2.caption(f"Entered {fmt_ts(p.get('entry_time'))}")

    with col_r:
        st.subheader("Today's Closed Trades")
        closed = q("c_positions", filters={"close_date": today, "status": "closed"}, order="-close_time")
        if not closed:
            st.info("No closed trades today")
        else:
            for p in closed:
                rpnl = p.get("realized_pnl", 0)
                color = "#3fb950" if rpnl >= 0 else "#f85149"
                with st.container(border=True):
                    c1, c2, c3 = st.columns([2, 2, 1])
                    c1.markdown(f"**{p['ticker']}**")
                    c2.markdown(f"${p['entry_price']:.2f} → closed `{p.get('exit_reason','—')}`")
                    c3.markdown(f"<span style='color:{color}'>{fmt_pnl(rpnl)}</span>", unsafe_allow_html=True)

    st.subheader("Recent Sessions (7 days)")
    sessions = q("c_sessions", order="-date", limit=14)
    if sessions:
        rows = []
        for s in sessions:
            rows.append({
                "Date":       s.get("date"),
                "Terminal":   s.get("terminal_reason"),
                "Proposed":   s.get("trades_proposed", 0),
                "Approved":   s.get("trades_approved", 0),
                "Executed":   s.get("trades_executed", 0),
                "Cost $":     round(s.get("total_cost_usd", 0), 4),
                "Tokens In":  s.get("total_tokens_input", 0),
                "Tokens Out": s.get("total_tokens_output", 0),
                "Latency s":  round(s.get("total_latency_ms", 0) / 1000, 1),
            })
        st.dataframe(rows, use_container_width=True, hide_index=True)
    else:
        st.info("No sessions yet")


# ══════════════════════════════════════════════════════════════════════════════
# PAGE: POSITIONS
# ══════════════════════════════════════════════════════════════════════════════

elif page == "Positions":
    st.header("Positions")

    filter_col, date_col, _ = st.columns([1, 1, 2])
    status_filter = filter_col.selectbox("Status", ["all", "open", "closed", "eod_forced"])
    days_back     = date_col.number_input("Days back", min_value=1, max_value=90, value=7)

    since = (date.today() - timedelta(days=days_back)).isoformat()

    db = get_client()
    req = db.table("c_positions").select("*").gte("open_date", since)
    if status_filter != "all":
        req = req.eq("status", status_filter)
    positions = req.order("open_date", desc=True).execute().data or []

    if not positions:
        st.info("No positions found")
    else:
        rows = []
        for p in positions:
            rpnl = p.get("realized_pnl")
            rows.append({
                "Ticker":    p["ticker"],
                "Status":    p["status"],
                "Context":   p.get("entry_context", "—"),
                "Entry $":   p["entry_price"],
                "Target $":  p["target_price"],
                "Stop $":    p["stop_loss"],
                "Shares":    p["shares"],
                "Size $":    round(p["position_size"], 0),
                "P&L":       fmt_pnl(rpnl),
                "Exit":      p.get("exit_reason", "—"),
                "Opened":    fmt_ts(p.get("entry_time")),
                "Closed":    fmt_ts(p.get("close_time")),
                "Score":     p.get("score_at_entry"),
            })
        st.dataframe(rows, use_container_width=True, hide_index=True)
        st.caption(f"{len(rows)} positions")


# ══════════════════════════════════════════════════════════════════════════════
# PAGE: OBSERVABILITY
# ══════════════════════════════════════════════════════════════════════════════

elif page == "Observability":
    st.header("Observability — Trace Explorer")

    sessions = q("c_sessions", order="-date", limit=30)
    if not sessions:
        st.info("No sessions yet")
        st.stop()

    session_labels = {
        s["id"]: f"{s['date']} · {s.get('terminal_reason','?')} · {s.get('agents_invoked', [])}"
        for s in sessions
    }
    selected_id = st.selectbox(
        "Session",
        options=list(session_labels.keys()),
        format_func=lambda x: session_labels[x],
    )

    selected_session = next(s for s in sessions if s["id"] == selected_id)

    # Session summary
    with st.expander("Session summary", expanded=True):
        c1, c2, c3, c4, c5 = st.columns(5)
        c1.metric("Steps",       selected_session.get("total_steps", 0))
        c2.metric("Tool Calls",  selected_session.get("total_tool_calls", 0))
        c3.metric("Tokens In",   f"{selected_session.get('total_tokens_input', 0):,}")
        c4.metric("Tokens Out",  f"{selected_session.get('total_tokens_output', 0):,}")
        c5.metric("Cost",        f"${selected_session.get('total_cost_usd', 0):.4f}")
        d1, d2, d3 = st.columns(3)
        d1.metric("Latency",     f"{selected_session.get('total_latency_ms', 0)/1000:.1f}s")
        d2.metric("Proposed",    selected_session.get("trades_proposed", 0))
        d3.metric("Executed",    selected_session.get("trades_executed", 0))
        agents = selected_session.get("agents_invoked") or []
        st.markdown("**Agents invoked:** " + " · ".join(agents) if agents else "—")

    # Trace stream
    traces = q("c_traces", filters={"session_id": selected_id}, order="sequence")

    agent_filter = st.multiselect(
        "Filter by agent",
        options=sorted({t["agent"] for t in traces}),
        default=[],
    )
    type_filter = st.multiselect(
        "Filter by step type",
        options=sorted({t["step_type"] for t in traces}),
        default=[],
    )

    filtered = [
        t for t in traces
        if (not agent_filter or t["agent"] in agent_filter)
        and (not type_filter or t["step_type"] in type_filter)
    ]

    st.caption(f"{len(filtered)} of {len(traces)} spans")

    step_colors = {
        "tool_call":     "#388bfd",
        "agent_message": "#3fb950",
        "decision":      "#d29922",
        "error":         "#f85149",
    }

    for t in filtered:
        color  = step_colors.get(t["step_type"], "#888")
        label  = t.get("tool_name") or t.get("step_type")
        err    = t.get("error")
        tokens = f"{t.get('tokens_input',0)}→{t.get('tokens_output',0)} tok"
        lat    = f"{t.get('latency_ms',0)}ms"

        header = (
            f"**#{t['sequence']}** &nbsp; "
            f"<span style='color:{color}'>{t['agent']}</span> &nbsp; "
            f"`{label}` &nbsp; {lat} &nbsp; {tokens}"
        )
        if err:
            header += f" &nbsp; <span style='color:#f85149'>ERROR</span>"

        with st.expander(label=f"#{t['sequence']} {t['agent']} · {label}", expanded=False):
            st.markdown(header, unsafe_allow_html=True)
            if t.get("agent_reasoning"):
                st.markdown("**Reasoning**")
                st.markdown(t["agent_reasoning"])
            if t.get("tool_input"):
                st.markdown("**Input**")
                st.json(t["tool_input"])
            if t.get("tool_output"):
                st.markdown("**Output**")
                st.json(t["tool_output"])
            if t.get("outcome"):
                st.markdown(f"**Outcome:** `{t['outcome']}`")
            if err:
                st.error(err)
            st.caption(f"Span {t['span_id']} · {fmt_ts(t.get('created_at'))}")


# ══════════════════════════════════════════════════════════════════════════════
# PAGE: SESSIONS
# ══════════════════════════════════════════════════════════════════════════════

elif page == "Sessions":
    st.header("Session History")

    days = st.slider("Days back", 1, 60, 30)
    since = (date.today() - timedelta(days=days)).isoformat()

    db = get_client()
    sessions = db.table("c_sessions").select("*").gte("date", since).order("date", desc=True).execute().data or []

    if not sessions:
        st.info("No sessions in range")
    else:
        rows = []
        for s in sessions:
            rows.append({
                "Date":        s.get("date"),
                "Terminal":    s.get("terminal_reason"),
                "Agents":      ", ".join(s.get("agents_invoked") or []),
                "Steps":       s.get("total_steps", 0),
                "Tool Calls":  s.get("total_tool_calls", 0),
                "Proposed":    s.get("trades_proposed", 0),
                "Approved":    s.get("trades_approved", 0),
                "Executed":    s.get("trades_executed", 0),
                "Risk Rej":    s.get("risk_rejections", 0),
                "Tokens In":   s.get("total_tokens_input", 0),
                "Tokens Out":  s.get("total_tokens_output", 0),
                "Cost $":      round(s.get("total_cost_usd", 0), 5),
                "Latency s":   round(s.get("total_latency_ms", 0) / 1000, 1),
                "Started":     fmt_ts(s.get("started_at")),
            })
        st.dataframe(rows, use_container_width=True, hide_index=True)

    # Protection events
    st.subheader("Protection Events")
    events = q("c_protection_events", order="-created_at", limit=20)
    if events:
        ev_rows = []
        for e in events:
            ev_rows.append({
                "Date":        e.get("event_date"),
                "Tier":        e.get("tier"),
                "Field":       e.get("trigger_field"),
                "Value":       e.get("trigger_value"),
                "Threshold":   e.get("threshold"),
                "Action":      e.get("action"),
                "Description": e.get("description"),
                "Unlocked":    e.get("human_unlocked"),
            })
        st.dataframe(ev_rows, use_container_width=True, hide_index=True)
    else:
        st.info("No protection events")


# ══════════════════════════════════════════════════════════════════════════════
# PAGE: PARAMETERS
# ══════════════════════════════════════════════════════════════════════════════

elif page == "Parameters":
    st.header("Strategy Parameters")

    params = q("c_strategy_params", order="param_key")
    if params:
        for p in params:
            val      = p["param_value"]
            mn, mx   = p["min_bound"], p["max_bound"]
            pct      = (val - mn) / (mx - mn) if mx != mn else 0.5
            with st.container(border=True):
                c1, c2, c3 = st.columns([2, 3, 2])
                c1.markdown(f"**{p['param_key']}**")
                c2.progress(float(pct), text=f"{val}  (min {mn} · max {mx})")
                c3.caption(f"default {p['default_value']} · cooldown {p['cooldown_days']}d")
                if p.get("change_reason"):
                    c1.caption(p["change_reason"])
                if p.get("cooldown_until"):
                    st.warning(f"On cooldown until {p['cooldown_until']}", icon="⏳")
    else:
        st.info("No parameters found")

    st.subheader("Agent Config")
    configs = q("c_agent_config", order="applies_to")
    if configs:
        rows = []
        for c in configs:
            rows.append({
                "Key":        c["config_key"],
                "Value":      c["config_value"],
                "Applies To": c["applies_to"],
                "Active":     c["is_active"],
                "Note":       c.get("change_note", ""),
            })
        st.dataframe(rows, use_container_width=True, hide_index=True)


# ══════════════════════════════════════════════════════════════════════════════
# PAGE: GOALS & LEARNINGS
# ══════════════════════════════════════════════════════════════════════════════

elif page == "Goals & Learnings":
    st.header("Goals")

    goals = q("c_goals", filters={"status": "active"})
    if goals:
        for g in goals:
            target = g["target_value"]
            current = g.get("current_value", 0)
            pct = min(1.0, max(0.0, current / target)) if target != 0 else 0
            with st.container(border=True):
                c1, c2 = st.columns([3, 1])
                c1.markdown(f"**{g['goal_type']}**  ·  target {fmt_pnl(target)}")
                c1.progress(float(pct), text=f"Current: {fmt_pnl(current)}")
                c2.caption(f"By: {g.get('created_by')}")
                if g.get("evidence"):
                    st.caption(g["evidence"])
    else:
        st.info("No active goals")

    st.subheader("All Goals")
    all_goals = q("c_goals", order="-created_at", limit=20)
    if all_goals:
        rows = [{
            "Type":    g["goal_type"],
            "Target":  fmt_pnl(g["target_value"]),
            "Current": fmt_pnl(g.get("current_value", 0)),
            "Status":  g["status"],
            "By":      g["created_by"],
            "From":    g.get("effective_from"),
            "Until":   g.get("effective_until", "—"),
        } for g in all_goals]
        st.dataframe(rows, use_container_width=True, hide_index=True)

    st.header("Learnings")

    outcome_filter = st.selectbox("Outcome", ["all", "pending", "validated", "false_positive", "expired"])
    db = get_client()
    lq = db.table("c_learnings").select("*")
    if outcome_filter != "all":
        lq = lq.eq("outcome", outcome_filter)
    learnings = lq.order("created_at", desc=True).limit(50).execute().data or []

    if not learnings:
        st.info("No learnings yet")
    else:
        for lrn in learnings:
            conf_color = {"high": "#3fb950", "medium": "#d29922", "low": "#888"}.get(lrn["confidence"], "#888")
            with st.expander(
                f"{lrn['session_date']} · {lrn['learning_type']} · {lrn.get('entity_id') or 'portfolio'}",
                expanded=False,
            ):
                st.markdown(lrn["finding"])
                c1, c2, c3 = st.columns(3)
                c1.markdown(f"Confidence: <span style='color:{conf_color}'>{lrn['confidence']}</span>", unsafe_allow_html=True)
                c2.markdown(f"Outcome: `{lrn['outcome']}`")
                c3.markdown(f"Expires: {lrn.get('expires_date','—')}")
                if lrn.get("action_taken"):
                    st.caption(f"Action: {lrn['action_taken']}")
                if lrn.get("param_key"):
                    st.caption(f"Param change: {lrn['param_key']}  {lrn.get('old_param_value')} → {lrn.get('new_param_value')}")
                if lrn.get("requires_human_review"):
                    st.warning("Requires human review")
