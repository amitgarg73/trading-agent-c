#!/usr/bin/env python3
"""
Generate workflow_c.png for Trading Agent C.
Run: python3 generate_workflow_c.py
Deps: pip install graphviz  +  brew install graphviz
"""
from graphviz import Digraph

C_HAIKU  = '#2e6d9e'
C_SONNET = '#1a3a5c'
C_TOOL   = '#2b5aa0'
C_GATE   = '#b03020'
C_TRACE  = '#1e7e42'
C_POST   = '#5a3a8a'
C_SIM    = '#555555'
C_PAPER  = '#b55a1a'
C_WHITE  = 'white'

# Section header colours
H_SETUP  = '#9e9e9e'
H_MKT    = '#4a7ab5'
H_RES    = '#3a6da8'
H_RISK   = '#7a5ab0'
H_ORCH   = '#b08820'
H_POST   = '#6a4aa8'


def lbl(title, sub='', tsz=12, ssz=10):
    if sub:
        return (f'<<FONT POINT-SIZE="{tsz}"><B>{title}</B></FONT>'
                f'<BR/><FONT POINT-SIZE="{ssz}">{sub}</FONT>>')
    return f'<<FONT POINT-SIZE="{tsz}"><B>{title}</B></FONT>>'


def section(g, nid, label, color):
    """Wide colored section-header bar."""
    g.node(nid, f'<<FONT POINT-SIZE="13"><B>{label}</B></FONT>>',
           shape='rectangle', style='filled', fillcolor=color,
           fontcolor=C_WHITE, width='11', height='0.45', margin='0.1,0.05')


def gate(g, nid, title, sub=''):
    g.node(nid, lbl(title, sub, 11, 9), shape='diamond',
           fillcolor=C_GATE, fontcolor=C_WHITE, width='1.8', height='1.2')


def box(g, nid, title, sub='', color=C_TOOL):
    g.node(nid, lbl(title, sub), shape='box',
           style='rounded,filled', fillcolor=color, fontcolor=C_WHITE,
           width='9')


def stop(g, nid, label):
    g.node(nid, label, shape='ellipse', style='filled,dashed',
           fillcolor='#f8d7da', fontcolor='#721c24', color=C_GATE,
           fontsize='10')


def ok(g, nid, label):
    g.node(nid, label, shape='ellipse', style='filled',
           fillcolor='#d4edda', fontcolor='#155724', fontsize='10')


def no_edge(g, src, dst, label=''):
    g.edge(src, dst, label=label, style='dashed',
           color=C_GATE, fontcolor=C_GATE, fontsize='9')


def trace_note(g, nid, text):
    g.node(nid, text, shape='note', style='filled',
           fillcolor='#c8e6c9', fontcolor='#1b5e20', fontsize='9',
           width='8')


# ─────────────────────────────────────────────────────────────────────────────
dot = Digraph('wf_c', format='png')
dot.attr(rankdir='TB', size='14,56', dpi='160', fontname='Helvetica',
         nodesep='0.35', ranksep='0.5', bgcolor='white', pad='0.6')
dot.attr('node', fontname='Helvetica', style='filled',
         margin='0.25,0.15', fontsize='11')
dot.attr('edge', fontname='Helvetica', fontsize='9', color='#555555',
         fontcolor='#333333', arrowsize='0.9')

# ── Title ─────────────────────────────────────────────────────────────────────
dot.node('_title',
    lbl('Trading Agent C — Agentic Multi-Agent Workflow',
        'Sequential orchestration · Tool use per agent · Full trace observability',
        19, 12),
    shape='rectangle', style='filled', fillcolor='white',
    fontcolor='#1a3a5c', color='white', width='11')

# ════════════════════════════════════════════════════════════════════════════
# ① SESSION SETUP
# ════════════════════════════════════════════════════════════════════════════
section(dot, 'h_setup', '① Session Setup', H_SETUP)
gate(dot, 'g_td',   'Trading Day?',      'weekend / holiday')
gate(dot, 'g_time', 'After 10:20 AM ET?', 'hard cap — late entries are negative EV')
ok(dot,   's_go',   'session start')
stop(dot, 'exit_s', 'exit — no session')

dot.edge('g_td',   'g_time', label='yes',    fontsize='9')
dot.edge('g_time', 's_go',   label='OK',     fontsize='9')
no_edge(dot, 'g_td',   'exit_s', 'NO')
no_edge(dot, 'g_time', 'exit_s', 'too late')

# ════════════════════════════════════════════════════════════════════════════
# ② MARKET AGENT
# ════════════════════════════════════════════════════════════════════════════
section(dot, 'h_mkt', '② Market Agent  (Claude Haiku)  —  all 4 tools required · 1 round · no loops', H_MKT)
box(dot, 'ma_tools',
    'All 4 tools called in sequence',
    'get_vix  (value · LOW/ELEVATED/HIGH/CRISIS/EXTREME)<BR/>'
    'get_futures  (S&amp;P · Nasdaq · Dow % change · avg_bias)<BR/>'
    'get_fear_greed  (value 0–100 · Extreme Fear→Extreme Greed)<BR/>'
    'get_sector_rotation  (all 11 sector ETFs sorted best→worst)',
    C_HAIKU)
gate(dot, 'g_mkt', 'Market decision?',
     'avg_futures &lt; -1.5% → SKIP<BR/>VIX &gt; 20 or F&amp;G &lt; 25 → CAUTION<BR/>otherwise → GO')
stop(dot, 'exit_skip', 'SKIP — 0 trades · session ends')
box(dot, 'mkt_out',
    'market_report  →  passed to Research Agent as user message',
    'decision (GO/CAUTION/SKIP) · max_positions · bias · summary',
    C_SONNET)
trace_note(dot, 'mn', 'c_traces: 4 tool call rows + 1 agent message row')

dot.edge('ma_tools', 'g_mkt')
no_edge(dot, 'g_mkt', 'exit_skip', 'SKIP')
dot.edge('g_mkt', 'mkt_out', label='GO / CAUTION', fontsize='9')
dot.edge('mkt_out', 'mn', style='dashed', color=C_TRACE, arrowsize='0.6')

# ════════════════════════════════════════════════════════════════════════════
# ③ RESEARCH AGENT
# ════════════════════════════════════════════════════════════════════════════
section(dot, 'h_res',
    '③ Research Agent  (Claude Sonnet)  —  1 get_candidates + max 5 deep-dives (~26 tool calls max)',
    H_RES)
box(dot, 'ra_ctx',
    'Receives market_report as user message',
    'CAUTION → requires score ≥ 7 + above_vwap before proposing any trade',
    C_HAIKU)
box(dot, 'ra_cands',
    'Phase 1 — get_candidates(min_score=5)  [called exactly once]',
    'Returns: ticker · score · price only — NOT full signals · max 100 results<BR/>'
    'Key design difference from Strategy A: agents decides what to investigate',
    C_TOOL)
box(dot, 'ra_sel',
    'Agent selects top 5 tickers to investigate',
    'Core agentic judgment: Claude reads scores + prices, decides which tickers to deep-dive',
    C_SONNET)
box(dot, 'ra_ph2',
    'Phase 2 — Per-ticker investigation  (repeats for each of the 5 chosen tickers)',
    '1 get_candidates + max 5 tickers × up to 5 tools = max ~26 tool calls per session',
    '#3a6a9e')
box(dot, 'ra_news',
    'get_news()  [REQUIRED first for every ticker]',
    'blackout: true → drop ticker immediately, pick next candidate from list',
    C_TOOL)
gate(dot, 'g_bo', 'Earnings blackout?', 'earnings today or tomorrow')
stop(dot, 'ra_drop', 'drop ticker\npick next')
box(dot, 'ra_deep',
    'Optional tools  (agent decides which to call per ticker)',
    'get_intraday_signals  →  above_vwap · rs_vs_spy · today_pct_change<BR/>'
    'get_atr               →  atr_pct · orb_pct<BR/>'
    'get_position_history  →  win_rate · avg_pnl · last_exit (30 days)',
    C_TOOL)
box(dot, 'ra_out',
    'trade_proposals  →  passed to Risk Agent as user message',
    'ticker · entry_price · target_price · stop_loss · position_size · confidence (HIGH/MEDIUM/LOW) · evidence[]',
    C_SONNET)
trace_note(dot, 'rn', 'c_traces: 1 candidates call + tool call rows per ticker + skipped[] outcomes')

dot.edge('ra_ctx',  'ra_cands')
dot.edge('ra_cands','ra_sel')
dot.edge('ra_sel',  'ra_ph2')
dot.edge('ra_ph2',  'ra_news')
dot.edge('ra_news', 'g_bo')
no_edge(dot, 'g_bo', 'ra_drop', 'YES')
dot.edge('ra_drop', 'ra_ph2', label='next ticker', style='dashed',
         color='#888888', fontsize='9', constraint='false')
dot.edge('g_bo',    'ra_deep', label='NO → investigate', fontsize='9')
dot.edge('ra_deep', 'ra_out')
dot.edge('ra_out',  'rn', style='dashed', color=C_TRACE, arrowsize='0.6')

# ════════════════════════════════════════════════════════════════════════════
# ④ RISK AGENT
# ════════════════════════════════════════════════════════════════════════════
section(dot, 'h_risk',
    '④ Risk Agent  (Claude Haiku)  —  all 4 portfolio tools required before reviewing any proposal',
    H_RISK)
box(dot, 'rk_tools',
    'All 4 portfolio tools called before reviewing proposals',
    'get_open_positions      →  ticker · size · sector · unrealized P&amp;L<BR/>'
    'get_today_pnl           →  realized_pnl · trades_closed · limit_hit<BR/>'
    'get_buying_power        →  buying_power · total_capital · deployed<BR/>'
    'get_portfolio_exposure  →  positions_open · by_sector · max_sector_pct',
    C_HAIKU)
box(dot, 'rk_rev',
    'Review each proposal — constraints applied in order',
    '① loss limit already hit → reject ALL<BR/>'
    '② insufficient buying power → reject<BR/>'
    '③ adding position pushes sector &gt; 35% → reject<BR/>'
    '④ ticker already in open positions → reject (duplicate)<BR/>'
    '⑤ position count already at MAX_POSITIONS cap → reject',
    C_HAIKU)
box(dot, 'rk_out',
    'risk_verdicts  →  passed to Orchestrator  (accumulated with market_report + trade_proposals)',
    'per ticker: APPROVED | REJECTED + specific constraint violated',
    '#3a1a6e')
trace_note(dot, 'rkn', 'c_traces: 4 tool call rows + 1 verdict row per ticker reviewed')

dot.edge('rk_tools', 'rk_rev')
dot.edge('rk_rev',   'rk_out')
dot.edge('rk_out',   'rkn', style='dashed', color=C_TRACE, arrowsize='0.6')

# ════════════════════════════════════════════════════════════════════════════
# ⑤ ORCHESTRATOR
# ════════════════════════════════════════════════════════════════════════════
section(dot, 'h_orch',
    '⑤ Orchestrator Agent  (Claude Sonnet)  —  no tools registered',
    H_ORCH)
box(dot, 'oc_in',
    'Receives accumulated context: market_report + trade_proposals + risk_verdicts',
    'Synthesizes — does not re-analyse, re-rank, or call tools',
    C_SONNET)
gate(dot, 'g_app', 'Any approved trades?')
box(dot, 'oc_retry',
    'Retry path — fixable rejections + iterations &lt; 2',
    'Fixable = sector concentration, position count limit<BR/>'
    'Research Agent called again with rejection context as user message<BR/>'
    'Same tool caps apply · Orchestrator synthesizes once more · no further retries',
    '#c05800')
stop(dot, 'oc_struct', 'structural block\n0 trades — session ends\nloss limit / no capital / no candidates')
box(dot, 'oc_out',
    'final_trade_list  (Strategy A-compatible output schema)',
    'trades[] · market_context · '
    'session_meta {loop_iterations · retry_triggered · terminal_reason}',
    C_SONNET)
trace_note(dot, 'ocn', 'c_traces: 1 decision row  ·  c_sessions: full session summary row')

dot.edge('oc_in',  'g_app')
dot.edge('g_app',  'oc_out',    label='YES → converged', fontsize='9')
no_edge(dot, 'g_app', 'oc_retry',  'NO — fixable')
no_edge(dot, 'g_app', 'oc_struct', 'NO — structural')
dot.edge('oc_out', 'ocn', style='dashed', color=C_TRACE, arrowsize='0.6')

# ════════════════════════════════════════════════════════════════════════════
# ⑥ POST-PROCESSING
# ════════════════════════════════════════════════════════════════════════════
section(dot, 'h_post',
    '⑥ Deterministic Post-Processing  (unchanged from Strategy A)',
    H_POST)
box(dot, 'atr',
    'ATR Sizer',
    'stop = ATR×0.8 · shares ≤ $150/mk · drops trade if R:R &lt; 1',
    C_POST)
box(dot, 'grail',
    'Guardrails',
    'Price sanity · duplicate check · capital cap · loss limit — final safety gate',
    C_POST)

with dot.subgraph() as r:
    r.attr(rank='same')
    dot.node('p_sim',
        lbl('Phase 1 — Simulation',
            'c_positions DB write only · no broker calls'),
        shape='box', style='rounded,filled', fillcolor=C_SIM, fontcolor=C_WHITE, width='4')
    dot.node('p_paper',
        lbl('Phase 2 — Paper Trading',
            'Alpaca bracket orders · stratc_{ticker}_{ts}<BR/>'
            'Separate Alpaca account · STRATEGY_TAG = "c"'),
        shape='box', style='rounded,filled', fillcolor=C_PAPER, fontcolor=C_WHITE, width='5')

dot.edge('atr',   'grail')
dot.edge('grail', 'p_sim')
dot.edge('grail', 'p_paper')

# ── Main spine — strictly vertical ───────────────────────────────────────────
dot.edge('_title',   'h_setup',  style='invis')
dot.edge('h_setup',  'g_td')
dot.edge('s_go',     'h_mkt',    style='invis')
dot.edge('h_mkt',    'ma_tools', label='→ Market Agent starts', fontsize='9')
dot.edge('mkt_out',  'h_res',    style='invis')
dot.edge('h_res',    'ra_ctx',   label='→ Research Agent starts', fontsize='9')
dot.edge('ra_out',   'h_risk',   style='invis')
dot.edge('h_risk',   'rk_tools', label='→ Risk Agent starts', fontsize='9')
dot.edge('rk_out',   'h_orch',   style='invis')
dot.edge('h_orch',   'oc_in',    label='→ Orchestrator starts', fontsize='9')
dot.edge('oc_out',   'h_post',   style='invis')
dot.edge('h_post',   'atr',      label='→ Post-processing', fontsize='9')

# ── Render ────────────────────────────────────────────────────────────────────
dot.render('workflow_c', cleanup=True, view=False)
print('Saved: workflow_c.png')
