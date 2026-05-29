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

# ── Password gate ──────────────────────────────────────────────────────────────

def _check_password() -> bool:
    if st.session_state.get("authenticated"):
        return True
    correct = st.secrets.get("DASHBOARD_PASSWORD", "")
    if not correct:
        return True  # no password configured — open access
    with st.form("login"):
        st.markdown("### Trading Agent C")
        pwd = st.text_input("Password", type="password")
        if st.form_submit_button("Sign in"):
            if pwd == correct:
                st.session_state["authenticated"] = True
                st.rerun()
            else:
                st.error("Incorrect password")
    st.stop()

_check_password()


# ── Connection ─────────────────────────────────────────────────────────────────

@st.cache_resource
def get_client() -> Client:
    url = os.environ.get("SUPABASE_URL") or st.secrets.get("SUPABASE_URL", "")
    key = os.environ.get("SUPABASE_KEY") or st.secrets.get("SUPABASE_KEY", "")
    if not url or not key:
        st.error("Set SUPABASE_URL and SUPABASE_KEY in your environment or .streamlit/secrets.toml")
        st.stop()
    return create_client(url, key)


@st.cache_resource
def get_client_ab() -> Client | None:
    url = os.environ.get("SUPABASE_URL_AB") or st.secrets.get("SUPABASE_URL_AB", "")
    key = os.environ.get("SUPABASE_KEY_AB") or st.secrets.get("SUPABASE_KEY_AB", "")
    if not url or not key:
        return None
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


def q_ab(table: str, cols: str = "*", filters: dict | None = None,
         gte: dict | None = None, order: str | None = None) -> list[dict]:
    try:
        db = get_client_ab()
        if db is None:
            return []
        req = db.table(table).select(cols)
        for k, v in (filters or {}).items():
            req = req.eq(k, v)
        for k, v in (gte or {}).items():
            req = req.gte(k, v)
        if order:
            desc = order.startswith("-")
            req = req.order(order.lstrip("-"), desc=desc)
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
        ["Today", "Overview", "P&L", "Costs", "Positions", "Observability", "Sessions", "Parameters", "Goals & Learnings", "Market Intel"],
        label_visibility="collapsed",
    )
    st.markdown("---")
    if st.button("Refresh", width="stretch"):
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
# PAGE: TODAY — consolidated view across all three strategies
# ══════════════════════════════════════════════════════════════════════════════

if page == "Today":
    st.header(f"Today — {today}")

    # ── Fetch data ──────────────────────────────────────────────────────────
    a_open   = q_ab("positions",   "ticker,entry_price,current_price,target_price,stop_loss,shares,unrealized_pnl,opened_at,close_reason,exit_mechanism,trail_order_id",
                    filters={"status": "OPEN"})
    a_closed = q_ab("positions",   "ticker,entry_price,close_price,shares,realized_pnl,opened_at,closed_at,close_reason",
                    filters={"status": "CLOSED"}, gte={"opened_at": today})
    b_open   = q_ab("b_positions", "ticker,pool,entry_price,current_price,target_price,stop_loss,shares,unrealized_pnl,opened_at,close_reason,exit_mechanism,trail_order_id",
                    filters={"status": "OPEN"})
    b_closed = q_ab("b_positions", "ticker,pool,entry_price,close_price,shares,realized_pnl,opened_at,closed_at,close_reason",
                    filters={"status": "CLOSED"}, gte={"opened_at": today})
    c_open   = [p for p in open_pos if p.get("open_date") == today]
    c_closed = q("c_positions",    "ticker,entry_price,exit_price,shares,realized_pnl,entry_time,close_time,exit_reason",
                 filters={"status": "closed", "close_date": today})

    # ── Summary bar ─────────────────────────────────────────────────────────
    def _sum(rows, key):
        return sum(r.get(key) or 0 for r in rows)

    a_unreal = _sum(a_open,   "unrealized_pnl")
    b_unreal = _sum(b_open,   "unrealized_pnl")
    c_unreal = _sum(c_open,   "unrealized_pnl")
    a_real   = _sum(a_closed, "realized_pnl")
    b_real   = _sum(b_closed, "realized_pnl")
    c_real   = _sum(c_closed, "realized_pnl")

    total_real   = a_real   + b_real   + c_real
    total_unreal = a_unreal + b_unreal + c_unreal

    s1, s2, s3, s4, s5 = st.columns(5)
    s1.metric("Total Realized P&L",   fmt_pnl(total_real),
              delta=f"{fmt_pnl(total_unreal)} unrealized", delta_color="normal")
    s2.metric("Strategy A",  f"{len(a_open)} open",
              delta=fmt_pnl(a_real + a_unreal), delta_color="normal")
    s3.metric("Strategy B",  f"{len(b_open)} open",
              delta=fmt_pnl(b_real + b_unreal), delta_color="normal")
    s4.metric("Strategy C",  f"{len(c_open)} open",
              delta=fmt_pnl(c_real + c_unreal), delta_color="normal")
    s5.metric("Closed Today", len(a_closed) + len(b_closed) + len(c_closed))

    st.markdown("---")

    # ── Per-strategy sections ───────────────────────────────────────────────
    def _pct_to_target(entry, target):
        if not entry or not target or entry == 0:
            return "—"
        pct = (target - entry) / entry * 100
        return f"{pct:+.1f}%"

    def _exit_badge(reason):
        if not reason:
            return ""
        colors = {"STOP": "#f85149", "TARGET": "#3fb950", "TRAIL": "#3fb950",
                  "NATIVE_TRAIL": "#3fb950", "EOD": "#888", "UNFILLED": "#888",
                  "MANUAL": "#888", "eod_forced": "#888", "stale_midnight_catchup": "#888"}
        color = colors.get(str(reason).upper(), "#d29922")
        label = str(reason).replace("_", " ").upper()
        return badge(label, color)

    def _trail_badge(row):
        if row.get("trail_order_id") or row.get("exit_mechanism") in ("NATIVE_TRAIL", "TRAIL"):
            return badge("TRAIL", "#d29922")
        return ""

    col_a, col_b, col_c = st.columns(3)

    # ── Strategy A ──────────────────────────────────────────────────────────
    with col_a:
        st.subheader("Strategy A")
        if a_open:
            for p in a_open:
                unreal = p.get("unrealized_pnl") or 0
                color  = "#3fb950" if unreal >= 0 else "#f85149"
                st.markdown(
                    f"**{p['ticker']}** &nbsp; {_trail_badge(p)}<br>"
                    f"<span style='font-size:0.85rem'>"
                    f"Entry ${p.get('entry_price', 0):.2f} · "
                    f"Now ${p.get('current_price') or 0:.2f} · "
                    f"{p.get('shares', 0)} sh<br>"
                    f"Stop ${p.get('stop_loss', 0):.2f} · "
                    f"Target ${p.get('target_price', 0):.2f} "
                    f"({_pct_to_target(p.get('current_price'), p.get('target_price'))})<br>"
                    f"<span style='color:{color};font-weight:600'>{fmt_pnl(unreal)}</span> unrealized"
                    f"</span>",
                    unsafe_allow_html=True,
                )
                st.markdown("")
        else:
            st.caption("No open positions")

        if a_closed:
            st.markdown("**Closed today**")
            for p in a_closed:
                real  = p.get("realized_pnl") or 0
                rc    = "#3fb950" if real >= 0 else "#f85149"
                st.markdown(
                    f"{p['ticker']} &nbsp; {_exit_badge(p.get('close_reason'))} &nbsp;"
                    f"<span style='color:{rc}'>"
                    f"**{fmt_pnl(real)}**</span> "
                    f"<span style='font-size:0.8rem;color:#888'>"
                    f"@ ${p.get('close_price') or 0:.2f}</span>",
                    unsafe_allow_html=True,
                )
        elif not a_open:
            st.caption("No trades today")

    # ── Strategy B ──────────────────────────────────────────────────────────
    with col_b:
        st.subheader("Strategy B")
        if b_open:
            for p in b_open:
                unreal = p.get("unrealized_pnl") or 0
                color  = "#3fb950" if unreal >= 0 else "#f85149"
                pool_badge = badge(f"Pool {p.get('pool', '?')}", "#388bfd")
                st.markdown(
                    f"**{p['ticker']}** &nbsp; {pool_badge} &nbsp; {_trail_badge(p)}<br>"
                    f"<span style='font-size:0.85rem'>"
                    f"Entry ${p.get('entry_price', 0):.2f} · "
                    f"Now ${p.get('current_price') or 0:.2f} · "
                    f"{p.get('shares', 0)} sh<br>"
                    f"Stop ${p.get('stop_loss', 0):.2f} · "
                    f"Target ${p.get('target_price', 0):.2f} "
                    f"({_pct_to_target(p.get('current_price'), p.get('target_price'))})<br>"
                    f"<span style='color:{color};font-weight:600'>{fmt_pnl(unreal)}</span> unrealized"
                    f"</span>",
                    unsafe_allow_html=True,
                )
                st.markdown("")
        else:
            st.caption("No open positions")

        if b_closed:
            st.markdown("**Closed today**")
            for p in b_closed:
                real       = p.get("realized_pnl") or 0
                rc         = "#3fb950" if real >= 0 else "#f85149"
                pool_badge = badge(f"Pool {p.get('pool', '?')}", "#388bfd")
                st.markdown(
                    f"{p['ticker']} &nbsp; {pool_badge} &nbsp; {_exit_badge(p.get('close_reason'))} &nbsp;"
                    f"<span style='color:{rc}'>"
                    f"**{fmt_pnl(real)}**</span> "
                    f"<span style='font-size:0.8rem;color:#888'>"
                    f"@ ${p.get('close_price') or 0:.2f}</span>",
                    unsafe_allow_html=True,
                )
        elif not b_open:
            st.caption("No trades today")

    # ── Strategy C ──────────────────────────────────────────────────────────
    with col_c:
        st.subheader("Strategy C")
        if c_open:
            for p in c_open:
                unreal = p.get("unrealized_pnl") or 0
                color  = "#3fb950" if unreal >= 0 else "#f85149"
                conf   = p.get("confidence", "")
                conf_color = {"HIGH": "#3fb950", "MEDIUM": "#d29922", "LOW": "#888"}.get(conf, "#888")
                st.markdown(
                    f"**{p['ticker']}** &nbsp; "
                    f"<span style='color:{conf_color};font-size:0.75rem'>{conf}</span><br>"
                    f"<span style='font-size:0.85rem'>"
                    f"Entry ${p.get('entry_price', 0):.2f} · "
                    f"{p.get('shares', 0)} sh<br>"
                    f"Stop ${p.get('stop_loss', 0):.2f} · "
                    f"Target ${p.get('target_price', 0):.2f} "
                    f"({_pct_to_target(p.get('entry_price'), p.get('target_price'))})<br>"
                    f"<span style='color:{color};font-weight:600'>{fmt_pnl(unreal)}</span> unrealized"
                    f"</span>",
                    unsafe_allow_html=True,
                )
                st.markdown("")
        else:
            st.caption("No open positions")

        if c_closed:
            st.markdown("**Closed today**")
            for p in c_closed:
                real = p.get("realized_pnl") or 0
                rc   = "#3fb950" if real >= 0 else "#f85149"
                st.markdown(
                    f"{p['ticker']} &nbsp; {_exit_badge(p.get('exit_reason'))} &nbsp;"
                    f"<span style='color:{rc}'>"
                    f"**{fmt_pnl(real)}**</span> "
                    f"<span style='font-size:0.8rem;color:#888'>"
                    f"@ ${p.get('exit_price') or 0:.2f}</span>",
                    unsafe_allow_html=True,
                )
        elif not c_open:
            st.caption("No trades today")

    st.markdown("---")

    # ── Session activity strip (C only, since A/B don't have session logs) ──
    c_session = q("c_sessions", filters={"date": today}, order="-started_at", limit=1)
    if c_session:
        sess = c_session[0]
        s = sess
        st.markdown(
            f"**Strategy C session** · "
            f"{s.get('terminal_reason','—')} · "
            f"{s.get('trades_executed',0)} executed · "
            f"{s.get('agents_invoked') or []} · "
            f"Cost ${s.get('total_cost_usd') or 0:.3f} · "
            f"Started {fmt_ts(s.get('started_at'))}",
            unsafe_allow_html=False,
        )


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
        st.dataframe(rows, width="stretch", hide_index=True)
    else:
        st.info("No sessions yet")


# ══════════════════════════════════════════════════════════════════════════════
# PAGE: P&L
# ══════════════════════════════════════════════════════════════════════════════

elif page == "P&L":
    st.header("P&L Dashboard")

    # ── Alpaca account equity ─────────────────────────────────────────────────
    api_key    = os.environ.get("ALPACA_API_KEY_ID_C") or st.secrets.get("ALPACA_API_KEY_ID_C", "")
    api_secret = os.environ.get("ALPACA_API_SECRET_KEY_C") or st.secrets.get("ALPACA_API_SECRET_KEY_C", "")

    col_eq, col_bp, col_cash = st.columns(3)
    if api_key and api_secret:
        try:
            from alpaca.trading.client import TradingClient
            _tc   = TradingClient(api_key, api_secret, paper=True)
            _acct = _tc.get_account()
            equity       = round(float(_acct.equity), 2)
            buying_power = round(float(_acct.buying_power), 2)
            cash         = round(float(_acct.cash), 2)
            start_equity = 100_000.0
            total_pnl    = round(equity - start_equity, 2)
            col_eq.metric("Account Equity",  f"${equity:,.2f}",
                          delta=f"${total_pnl:+,.2f} vs start")
            col_bp.metric("Buying Power",    f"${buying_power:,.2f}")
            col_cash.metric("Cash",          f"${cash:,.2f}")
        except Exception as e:
            col_eq.warning(f"Alpaca unavailable: {e}")
    else:
        col_eq.info("Set ALPACA_API_KEY_ID_C + ALPACA_API_SECRET_KEY_C to see live equity")

    st.markdown("---")

    # ── Daily P&L bar chart ───────────────────────────────────────────────────
    st.subheader("Daily P&L (last 30 days)")
    perf_history = q("c_daily_performance", order="-date", limit=30)
    if perf_history:
        import pandas as pd
        df_perf = pd.DataFrame([
            {"Date": r["date"], "P&L": r.get("realized_pnl", 0.0)}
            for r in reversed(perf_history)
        ])
        colors = ["#3fb950" if v >= 0 else "#f85149" for v in df_perf["P&L"]]
        st.bar_chart(df_perf.set_index("Date")["P&L"])

        # Summary stats
        s1, s2, s3, s4 = st.columns(4)
        days_traded = len(df_perf)
        winning     = (df_perf["P&L"] > 0).sum()
        total_pnl_db = df_perf["P&L"].sum()
        avg_daily    = df_perf["P&L"].mean()
        s1.metric("Days Traded",   days_traded)
        s2.metric("Winning Days",  f"{winning}/{days_traded}")
        s3.metric("Total P&L",     f"${total_pnl_db:+,.2f}")
        s4.metric("Avg Daily P&L", f"${avg_daily:+,.2f}")
    else:
        st.info("No performance history yet — runs after first EOD session")

    st.markdown("---")

    # ── Open positions with live unrealized P&L ───────────────────────────────
    st.subheader("Open Positions")
    open_positions = q("c_positions", filters={"status": "open"})
    if not open_positions:
        st.info("No open positions")
    else:
        live_pnl = {}
        if api_key and api_secret:
            try:
                alpaca_positions = _tc.get_all_positions()
                live_pnl = {p.symbol: round(float(p.unrealized_pl), 2)
                            for p in alpaca_positions}
            except Exception:
                pass

        rows = []
        for p in open_positions:
            ticker    = p["ticker"]
            entry     = p.get("entry_price", 0.0)
            target    = p.get("target_price", 0.0)
            stop      = p.get("stop_loss", 0.0)
            shares    = p.get("shares", 0)
            unr_pnl   = live_pnl.get(ticker)
            rows.append({
                "Ticker":        ticker,
                "Shares":        shares,
                "Entry":         f"${entry:.2f}",
                "Target":        f"${target:.2f}",
                "Stop":          f"${stop:.2f}",
                "Unrealized P&L": f"${unr_pnl:+.2f}" if unr_pnl is not None else "—",
                "Order ID":      (p.get("alpaca_order_id") or "")[:12] + "…" if p.get("alpaca_order_id") else "—",
            })
        st.dataframe(rows, width="stretch", hide_index=True)

    st.markdown("---")

    # ── Closed trades (last 30 days) ──────────────────────────────────────────
    st.subheader("Closed Trades (last 30 days)")
    cutoff = (date.today() - timedelta(days=30)).isoformat()
    closed = q("c_positions",
               filters={"status": "closed"},
               order="-close_date",
               limit=100)
    closed = [r for r in closed if (r.get("close_date") or "") >= cutoff]

    if not closed:
        st.info("No closed trades in the last 30 days")
    else:
        trade_rows = []
        for r in closed:
            pnl = r.get("realized_pnl") or 0.0
            trade_rows.append({
                "Date":      r.get("close_date", ""),
                "Ticker":    r["ticker"],
                "Shares":    r.get("shares", ""),
                "Entry":     f"${r.get('entry_price', 0):.2f}",
                "P&L":       f"${pnl:+.2f}",
                "Exit":      r.get("exit_reason", ""),
                "Context":   r.get("entry_context", "premarket"),
            })
        st.dataframe(trade_rows, width="stretch", hide_index=True)

        wins  = sum(1 for r in closed if (r.get("realized_pnl") or 0) > 0)
        total = len(closed)
        total_closed_pnl = sum(r.get("realized_pnl") or 0 for r in closed)
        c1, c2, c3 = st.columns(3)
        c1.metric("Trades",   total)
        c2.metric("Win Rate", f"{wins/total:.0%}" if total else "—")
        c3.metric("Total P&L", f"${total_closed_pnl:+,.2f}")


# ══════════════════════════════════════════════════════════════════════════════
# PAGE: COSTS
# ══════════════════════════════════════════════════════════════════════════════

elif page == "Costs":
    import pandas as pd

    st.header("Claude API Cost Tracking")

    sessions = q("c_sessions", order="-date", limit=60)

    if not sessions:
        st.info("No sessions yet — costs will appear after the first premarket run")
    else:
        # ── KPIs ──────────────────────────────────────────────────────────────
        total_cost   = sum(s.get("total_cost_usd") or 0 for s in sessions)
        session_count = len(sessions)
        avg_cost     = total_cost / session_count if session_count else 0
        days         = len({s["date"] for s in sessions})
        daily_avg    = total_cost / days if days else 0

        k1, k2, k3, k4 = st.columns(4)
        k1.metric("Total Spent",      f"${total_cost:.4f}")
        k2.metric("Sessions",         session_count)
        k3.metric("Avg per Session",  f"${avg_cost:.4f}")
        k4.metric("Avg per Day",      f"${daily_avg:.4f}")

        st.markdown("---")

        # ── Daily cost bar chart ───────────────────────────────────────────────
        st.subheader("Daily Cost (last 30 days)")
        daily_map: dict[str, float] = {}
        for s in sessions:
            d = s.get("date", "")
            daily_map[d] = daily_map.get(d, 0) + (s.get("total_cost_usd") or 0)

        df_daily = pd.DataFrame(
            [{"Date": d, "Cost ($)": round(c, 6)} for d, c in sorted(daily_map.items())]
        ).tail(30)
        st.bar_chart(df_daily.set_index("Date"))

        st.markdown("---")

        # ── Per-agent cost breakdown ───────────────────────────────────────────
        st.subheader("Cost by Agent (all sessions)")
        agent_totals: dict[str, dict] = {}
        for s in sessions:
            breakdown = s.get("cost_breakdown") or {}
            for agent, data in breakdown.items():
                if agent not in agent_totals:
                    agent_totals[agent] = {"input": 0, "output": 0, "cache_read": 0,
                                           "cache_write": 0, "cost_usd": 0.0, "model": data.get("model", "")}
                agent_totals[agent]["input"]       += data.get("input", 0)
                agent_totals[agent]["output"]      += data.get("output", 0)
                agent_totals[agent]["cache_read"]  += data.get("cache_read", 0)
                agent_totals[agent]["cache_write"] += data.get("cache_write", 0)
                agent_totals[agent]["cost_usd"]    += data.get("cost_usd", 0.0)

        if agent_totals:
            agent_rows = [
                {
                    "Agent":        agent,
                    "Model":        v["model"].split("-")[1] if "-" in v["model"] else v["model"],
                    "Input tok":    f"{v['input']:,}",
                    "Output tok":   f"{v['output']:,}",
                    "Cache read":   f"{v['cache_read']:,}",
                    "Cache write":  f"{v['cache_write']:,}",
                    "Cost ($)":     f"${v['cost_usd']:.4f}",
                    "% of total":   f"{v['cost_usd']/total_cost*100:.1f}%" if total_cost else "—",
                }
                for agent, v in sorted(agent_totals.items(), key=lambda x: -x[1]["cost_usd"])
            ]
            st.dataframe(agent_rows, width="stretch", hide_index=True)
        else:
            st.info("Per-agent breakdown available after next session completes")

        st.markdown("---")

        # ── Session cost table ─────────────────────────────────────────────────
        st.subheader("Session History")
        session_rows = [
            {
                "Date":       s.get("date", ""),
                "Session":    str(s.get("id", ""))[:8] + "…",
                "Terminal":   s.get("terminal_reason", ""),
                "Tokens in":  f"{s.get('total_tokens_input', 0):,}",
                "Tokens out": f"{s.get('total_tokens_output', 0):,}",
                "Cost ($)":   f"${s.get('total_cost_usd') or 0:.4f}",
                "Trades":     s.get("trades_executed", 0),
            }
            for s in sessions
        ]
        st.dataframe(session_rows, width="stretch", hide_index=True)


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
        st.dataframe(rows, width="stretch", hide_index=True)
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

    traces = q("c_traces", filters={"session_id": selected_id}, order="sequence")

    STEP_COLORS = {
        "tool_call":     "#388bfd",
        "agent_message": "#3fb950",
        "decision":      "#d29922",
        "error":         "#f85149",
    }
    AGENT_ORDER = ["market", "news_analyst", "research", "risk", "orchestrator", "learning"]

    tab_graph, tab_timeline, tab_stream = st.tabs(["Call Chain Graph", "Timeline", "Trace Stream"])

    # ── TAB: CALL CHAIN GRAPH ─────────────────────────────────────────────────
    with tab_graph:
        if not traces:
            st.info("No traces for this session")
        else:
            import graphviz

            # Aggregate per-agent stats
            agent_stats: dict[str, dict] = {}
            for t in traces:
                a = t["agent"]
                if a not in agent_stats:
                    agent_stats[a] = {"steps": 0, "tool_calls": 0, "tokens_in": 0,
                                      "tokens_out": 0, "latency_ms": 0, "errors": 0}
                agent_stats[a]["steps"]      += 1
                agent_stats[a]["tokens_in"]  += t.get("tokens_input", 0)
                agent_stats[a]["tokens_out"] += t.get("tokens_output", 0)
                agent_stats[a]["latency_ms"] += t.get("latency_ms", 0)
                if t["step_type"] == "tool_call":
                    agent_stats[a]["tool_calls"] += 1
                if t["step_type"] == "error":
                    agent_stats[a]["errors"] += 1

            # Infer handoff edges from decision outcomes
            decisions = [t for t in traces if t["step_type"] == "decision"]
            handoffs: list[tuple[str, str, str]] = []
            outcome_map = {t["agent"]: t.get("outcome", "") for t in decisions}
            seq = [a for a in AGENT_ORDER if a in agent_stats]
            for i in range(len(seq) - 1):
                src, dst = seq[i], seq[i + 1]
                label = outcome_map.get(src, "→")
                handoffs.append((src, dst, label[:30]))

            g = graphviz.Digraph(graph_attr={"rankdir": "LR", "bgcolor": "transparent",
                                              "fontname": "Arial", "splines": "ortho"})
            g.attr("node", shape="box", style="rounded,filled", fontname="Arial",
                   fontsize="11", margin="0.3,0.15")
            g.attr("edge", fontname="Arial", fontsize="9", color="#666666")

            AGENT_COLORS = {
                "market":       ("#1f4e79", "#dce9f7"),
                "news_analyst": ("#4b2e83", "#ede9f7"),
                "research":     ("#1a4731", "#d4edda"),
                "risk":         ("#7c2d12", "#fde8d0"),
                "orchestrator": ("#374151", "#e5e7eb"),
                "learning":     ("#1e3a5f", "#dbeafe"),
            }

            for agent, stats in agent_stats.items():
                fc, bc = AGENT_COLORS.get(agent, ("#333", "#eee"))
                tok = f"{stats['tokens_in']:,}→{stats['tokens_out']:,} tok"
                lat = f"{stats['latency_ms']}ms"
                tools_str = f"{stats['tool_calls']} tools · " if stats["tool_calls"] else ""
                err_str   = f" ⚠ {stats['errors']} err" if stats["errors"] else ""
                node_label = f"{agent}\n{tools_str}{lat}\n{tok}{err_str}"
                g.node(agent, label=node_label, fillcolor=bc, fontcolor=fc, color=fc)

            for src, dst, label in handoffs:
                g.edge(src, dst, label=label)

            st.graphviz_chart(g, width="stretch")

            # Drill-down: pick an agent to inspect
            st.markdown("---")
            st.subheader("Drill-down by agent")
            agents_present = [a for a in AGENT_ORDER if a in agent_stats]
            selected_agent = st.selectbox("Select agent", agents_present, key="graph_agent")

            agent_traces = [t for t in traces if t["agent"] == selected_agent]
            st.caption(f"{len(agent_traces)} steps · "
                       f"{agent_stats[selected_agent]['tokens_in']:,} tokens in · "
                       f"{agent_stats[selected_agent]['tokens_out']:,} tokens out · "
                       f"{agent_stats[selected_agent]['latency_ms']}ms total")

            for t in agent_traces:
                color = STEP_COLORS.get(t["step_type"], "#888")
                label = t.get("tool_name") or t["step_type"]
                with st.expander(f"#{t['sequence']} `{label}` — {t.get('latency_ms',0)}ms", expanded=False):
                    st.markdown(f"<span style='color:{color}'>**{t['step_type']}**</span>",
                                unsafe_allow_html=True)
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
                    if t.get("step_type") == "error":
                        st.error(t.get("agent_reasoning", "unknown error"))
                    tok = f"{t.get('tokens_input',0)}→{t.get('tokens_output',0)}"
                    st.caption(f"seq {t['sequence']} · tokens {tok} · span {str(t['span_id'])[:8]}")

    # ── TAB: TIMELINE ─────────────────────────────────────────────────────────
    with tab_timeline:
        if not traces:
            st.info("No traces for this session")
        else:
            import plotly.graph_objects as go

            fig = go.Figure()

            for step_type, color in STEP_COLORS.items():
                pts = [t for t in traces if t["step_type"] == step_type]
                if not pts:
                    continue
                hover = [
                    (
                        f"<b>{t.get('tool_name') or t['step_type']}</b><br>"
                        f"outcome: {t.get('outcome') or '—'}<br>"
                        f"latency: {t.get('latency_ms', 0)}ms<br>"
                        f"tokens: {t.get('tokens_input', 0)}→{t.get('tokens_output', 0)}<br>"
                        f"{(t.get('agent_reasoning') or '')[:120]}"
                    )
                    for t in pts
                ]
                fig.add_trace(go.Scatter(
                    x=[t["sequence"] for t in pts],
                    y=[t["agent"] for t in pts],
                    mode="markers",
                    name=step_type,
                    marker=dict(
                        color=color,
                        size=[max(8, min(24, 8 + (t.get("tokens_output", 0) or 0) // 50)) for t in pts],
                        symbol="circle",
                        line=dict(width=1, color="#fff"),
                    ),
                    hovertext=hover,
                    hoverinfo="text",
                ))

            # Draw sequence connectors
            fig.add_trace(go.Scatter(
                x=[t["sequence"] for t in traces],
                y=[t["agent"] for t in traces],
                mode="lines",
                line=dict(color="#444", width=0.5, dash="dot"),
                showlegend=False,
                hoverinfo="skip",
            ))

            fig.update_layout(
                height=380,
                margin=dict(l=20, r=20, t=30, b=20),
                yaxis=dict(categoryorder="array", categoryarray=list(reversed(AGENT_ORDER)),
                           gridcolor="#333"),
                xaxis=dict(title="Sequence", gridcolor="#333"),
                paper_bgcolor="rgba(0,0,0,0)",
                plot_bgcolor="rgba(0,0,0,0)",
                legend=dict(orientation="h", y=1.1),
                font=dict(color="#ccc"),
            )
            st.plotly_chart(fig, width="stretch")
            st.caption("Marker size ∝ output tokens. Hover for details.")

    # ── TAB: TRACE STREAM ─────────────────────────────────────────────────────
    with tab_stream:
        agent_filter = st.multiselect(
            "Filter by agent",
            options=sorted({t["agent"] for t in traces}),
            default=[],
            key="stream_agent",
        )
        type_filter = st.multiselect(
            "Filter by step type",
            options=sorted({t["step_type"] for t in traces}),
            default=[],
            key="stream_type",
        )

        filtered = [
            t for t in traces
            if (not agent_filter or t["agent"] in agent_filter)
            and (not type_filter or t["step_type"] in type_filter)
        ]
        st.caption(f"{len(filtered)} of {len(traces)} spans")

        for t in filtered:
            color = STEP_COLORS.get(t["step_type"], "#888")
            label = t.get("tool_name") or t.get("step_type")
            err   = t.get("step_type") == "error"
            with st.expander(f"#{t['sequence']} {t['agent']} · {label}", expanded=False):
                st.markdown(
                    f"**#{t['sequence']}** &nbsp; "
                    f"<span style='color:{color}'>{t['agent']}</span> &nbsp; "
                    f"`{label}` &nbsp; {t.get('latency_ms',0)}ms &nbsp; "
                    f"{t.get('tokens_input',0)}→{t.get('tokens_output',0)} tok",
                    unsafe_allow_html=True,
                )
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
                    st.error(t.get("agent_reasoning", "error"))
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
        st.dataframe(rows, width="stretch", hide_index=True)

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
        st.dataframe(ev_rows, width="stretch", hide_index=True)
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
        st.dataframe(rows, width="stretch", hide_index=True)


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
        st.dataframe(rows, width="stretch", hide_index=True)

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


# ══════════════════════════════════════════════════════════════════════════════
# PAGE: MARKET INTEL (shadow agent comparison)
# ══════════════════════════════════════════════════════════════════════════════

elif page == "Market Intel":
    st.header("Market Agent — Shadow Evaluation")
    st.caption(
        "V1 = threshold-based (4 tools, hard rules).  "
        "V2 = autonomous (6 tools, circuit-breakers only).  "
        "V2 does not affect trade execution — comparison only."
    )

    evals = q("c_market_evals", order="-eval_date", limit=60)

    if not evals:
        st.info("No evaluation data yet. Data populates after the first premarket session.")
    else:
        total   = len(evals)
        agreed  = sum(1 for e in evals if e.get("decisions_agree"))
        cb_days = sum(1 for e in evals if e.get("v2_circuit_breaker"))
        v2_cons = sum(1 for e in evals if not e.get("decisions_agree") and
                      e.get("v2_decision") in ("SKIP", "CAUTION") and
                      e.get("v1_decision") == "GO")
        v1_cons = sum(1 for e in evals if not e.get("decisions_agree") and
                      e.get("v1_decision") in ("SKIP", "CAUTION") and
                      e.get("v2_decision") == "GO")

        m1, m2, m3, m4 = st.columns(4)
        m1.metric("Days Tracked",        total)
        m2.metric("Agreement Rate",      f"{agreed / total:.0%}" if total else "—")
        m3.metric("V2 More Conservative", v2_cons)
        m4.metric("Circuit Breakers Fired", cb_days)

        st.markdown("---")

        # Day-by-day table
        st.subheader("Day-by-Day Comparison")
        decision_icon = {"GO": "GO", "CAUTION": "CAUTION", "SKIP": "SKIP"}
        rows = []
        for e in evals:
            agree_str = "Yes" if e.get("decisions_agree") else "No"
            rows.append({
                "Date":         e["eval_date"],
                "V1 Decision":  e.get("v1_decision", "—"),
                "V1 MaxPos":    e.get("v1_max_positions"),
                "V1 Bias":      e.get("v1_bias", "—"),
                "V2 Decision":  e.get("v2_decision", "—"),
                "V2 MaxPos":    e.get("v2_max_positions"),
                "V2 Bias":      e.get("v2_bias", "—"),
                "V2 Conf":      e.get("v2_confidence", "—"),
                "Agree?":       agree_str,
                "CB Fired":     "Yes" if e.get("v2_circuit_breaker") else "No",
                "P&L":          fmt_pnl(e.get("session_pnl")),
                "Hindsight":    e.get("hindsight_call") or "pending",
            })
        st.dataframe(rows, width="stretch", hide_index=True)

        # Disagreement drill-down
        disagreements = [e for e in evals if not e.get("decisions_agree")]
        if disagreements:
            st.markdown("---")
            st.subheader(f"Disagreements ({len(disagreements)} days)")
            for e in disagreements:
                v2_cb = e.get("v2_circuit_breaker")
                label = (
                    f"{e['eval_date']}  —  V1: {e.get('v1_decision')} "
                    f"({e.get('v1_max_positions')} pos)  |  "
                    f"V2: {e.get('v2_decision')} ({e.get('v2_max_positions')} pos)"
                )
                with st.expander(label, expanded=False):
                    c1, c2 = st.columns(2)
                    with c1:
                        st.markdown("**V1 (Baseline)**")
                        st.markdown(e.get("v1_summary") or "—")
                    with c2:
                        st.markdown("**V2 (Shadow)**")
                        if v2_cb:
                            st.markdown(f"Circuit breaker: `{v2_cb}`")
                        else:
                            st.markdown(e.get("v2_summary") or "—")
                        factors = e.get("v2_key_factors") or []
                        if factors:
                            st.markdown("Key factors: " + " · ".join(f"`{f}`" for f in factors))
                    pnl = e.get("session_pnl")
                    if pnl is not None:
                        st.markdown(
                            f"Outcome: **{fmt_pnl(pnl)}**  ·  "
                            f"Hindsight: `{e.get('hindsight_call') or 'pending'}`"
                        )

        # V2 key factors frequency
        all_factors: list[str] = []
        for e in evals:
            all_factors.extend(e.get("v2_key_factors") or [])
        if all_factors:
            st.markdown("---")
            st.subheader("V2 Key Factors (frequency)")
            from collections import Counter
            factor_counts = Counter(all_factors).most_common(15)
            factor_rows = [{"Factor": f, "Count": c} for f, c in factor_counts]
            st.dataframe(factor_rows, width="stretch", hide_index=True)
