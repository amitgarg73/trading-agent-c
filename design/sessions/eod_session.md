# EOD Session — Design Doc

**File:** `sessions/eod.py`
**Type:** Python service (not an agent)
**Runs:** Once per day, Mon-Fri at 3:55 PM ET via GitHub Actions
**Purpose:** Force-close open positions, reconcile fills with Alpaca, run performance
calculations, check principal protection, update goals, trigger Learning Agent, send
daily summary alert.

---

## Session Flow

```
3:55 PM ET — strategy_c_eod.yml fires

1. GUARD CHECKS
   session_id = db.get_today_session_id()
   If not session_id:
     log "EOD: No premarket session today" and exit.
   If not is_trading_day():
     exit.

2. FORCE-CLOSE OPEN POSITIONS
   open_positions = db.get_open_positions(session_id)
   For each position:
     order = alpaca.submit_market_sell(
       symbol=position.ticker,
       qty=position.shares,
       time_in_force="day",
       client_order_id=f"stratc_eod_{position.ticker}_{session_id[:8]}"
     )
     db.mark_position_closing(position.id, order.id)

3. WAIT FOR FILLS (up to 10 minutes)
   deadline = now() + timedelta(minutes=10)
   While now() < deadline:
     pending = db.get_positions_with_status("closing")
     If not pending: break
     For each p in pending:
       fill = alpaca.get_order(p.closing_order_id)
       If fill.status == "filled":
         record_trade_exit(
           ticker=p.ticker,
           exit_price=fill.filled_avg_price,
           exit_time=fill.filled_at,
           exit_reason="eod_forced",
           session_id=session_id,
         )
         db.mark_position_closed(p.id)
     sleep(15)
   If any remain open after deadline: log warning, mark as "eod_timeout"

4. RECONCILE WITH ALPACA
   reconcile_result = reconcile_positions(session_id)
   For each discrepancy:
     if alpaca_says_closed and db_says_open:
       record_trade_exit(..., exit_reason="bracket_exit_detected")
     if alpaca_says_open and db_says_closed:
       log anomaly to c_traces, alert

5. CALCULATE DAILY PERFORMANCE
   trades = db.get_today_trades(session_id)
   perf = DailyPerformance(
     date=today,
     session_id=session_id,
     realized_pnl=sum(t.realized_pnl for t in trades),
     trades_total=len(trades),
     trades_won=sum(1 for t in trades if t.realized_pnl > 0),
     trades_lost=sum(1 for t in trades if t.realized_pnl <= 0),
     win_rate=trades_won / trades_total if trades_total > 0 else 0.0,
     largest_win=max(t.realized_pnl for t in trades) if trades else 0.0,
     largest_loss=min(t.realized_pnl for t in trades) if trades else 0.0,
     avg_hold_minutes=avg(exit_time - entry_time for t in trades) if trades else 0,
     avg_rr_achieved=avg(t.rr_achieved for t in trades) if trades else 0.0,
   )
   db.upsert_daily_performance(perf)

6. PRINCIPAL PROTECTION CHECK
   protection_events = check_protection_tiers(perf)
   For each event:
     db.insert_protection_event(event)
     If event.tier >= 4:
       alerts.send_urgent(f"Protection Tier {event.tier} triggered: {event.action}")

7. UPDATE GOALS
   For each active goal in c_goals:
     updated_value = compute_goal_progress(goal, perf)
     db.update_goal_current_value(goal.id, updated_value)
     db.insert_goal_snapshot(goal.id, today, updated_value)
   evaluate_recent_param_adjustments()  # validate/revert Learning Agent changes

8. RUN LEARNING AGENT (conditional)
   config = load_agent_config()
   If config.get("enable_learning_agent", True) and perf.trades_total > 0:
     learnings = run_learning_agent(session_id, perf, tracer)
     If learnings.params_adjusted > 0:
       alerts.send(f"Learning Agent adjusted {learnings.params_adjusted} param(s): "
                   f"{learnings.top_finding}")
     If learnings.goal_recommended:
       alerts.send("New goal recommended — review in c_goals")
   Else:
     learnings = None

9. SEND DAILY SUMMARY
   send_daily_summary(perf, learnings)

10. FINALIZE SESSION
    db.update_session_completed_at(session_id)
    log f"EOD complete: {perf.realized_pnl:.2f} P&L, {perf.trades_total} trades"
```

---

## Reconciliation Logic

Reconciliation catches positions closed by Alpaca bracket orders during the day
that the intraday poller may have missed (e.g., if the poller was down during a fill).

```python
def reconcile_positions(session_id: str) -> ReconcileResult:
    """
    Compare DB open positions against Alpaca current state.
    Fix discrepancies. Return summary of what was corrected.
    """
    alpaca_open = {p.symbol: p for p in alpaca.list_positions()}
    db_open = {p.ticker: p for p in db.get_open_positions(session_id)}

    discrepancies = []

    # Closed in Alpaca but still open in DB
    for ticker, db_pos in db_open.items():
        if ticker not in alpaca_open:
            # Alpaca closed it — find the order to get fill price
            orders = alpaca.get_orders(
                symbol=ticker,
                after=db_pos.entry_time,
                status="filled"
            )
            exit_order = next((o for o in orders if o.side == "sell"), None)
            if exit_order:
                record_trade_exit(
                    ticker=ticker,
                    exit_price=float(exit_order.filled_avg_price),
                    exit_time=exit_order.filled_at,
                    exit_reason=_infer_exit_reason(exit_order),
                    session_id=session_id,
                )
                discrepancies.append({"ticker": ticker, "type": "missed_exit_corrected"})
            else:
                discrepancies.append({"ticker": ticker, "type": "alpaca_closed_no_fill_found"})

    # Open in Alpaca but closed in DB (rare anomaly)
    for symbol, alpaca_pos in alpaca_open.items():
        if symbol.startswith("stratc_") and symbol not in db_open:
            discrepancies.append({"ticker": symbol, "type": "orphan_alpaca_position"})
            alerts.send(f"Reconciliation: orphan position found in Alpaca: {symbol}")

    return ReconcileResult(discrepancies=discrepancies)
```

---

## Daily Performance Record (c_daily_performance)

```sql
c_daily_performance (
  id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  session_id      UUID,
  date            DATE,
  realized_pnl    FLOAT,
  trades_total    INT,
  trades_won      INT,
  trades_lost     INT,
  win_rate        FLOAT,
  largest_win     FLOAT,
  largest_loss    FLOAT,
  avg_hold_min    FLOAT,
  avg_rr_achieved FLOAT,
  vix_at_open     FLOAT,
  market_signal   TEXT,
  protection_tier INT,       -- highest tier triggered today, 0 if none
  created_at      TIMESTAMPTZ DEFAULT now()
)
```

---

## Daily Summary Alert

Sent to Gmail (same alert channel as A/B). Format:

```
Subject: Strategy C — Daily Summary 2026-05-27

P&L: +$284.00  |  Trades: 3W / 1L  |  Win Rate: 75%

Winners:
  AAPL  +$112.00  (target hit, held 2h 14m)
  NVDA   +$96.00  (target hit, held 1h 42m)
  AMD    +$76.00  (target hit, held 3h 05m)

Losers:
  CRWD  -$0.00  (stopped out, held 47m)  [Hmm — stop = entry here, re-check]

Cost:  $0.052 (no retry)
Session: premarket converged, 1 iteration

Learning Agent:
  Wrote 2 observations, 0 adjustments.
  Context for tomorrow: Technology sector 100% win rate today (3/3). CRWD
  stopped out at entry — possible wide spread at open. Watch entry type.

Goals:
  Daily target $300: $284 — missed by $16
```

If protection tier >= 3 triggered, subject prefix changes to `[ALERT]`.

---

## GitHub Actions Workflow (strategy_c_eod.yml)

```yaml
name: Strategy C — EOD
on:
  schedule:
    # 3:55 PM ET Mon-Fri = 20:55 UTC
    - cron: "55 20 * * 1-5"

jobs:
  eod:
    runs-on: ubuntu-latest
    timeout-minutes: 20
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"
      - uses: actions/setup-node@v4
        with:
          node-version: "20"
      - run: pip install -r requirements.txt
      - run: cd agents/ts && npm ci && npm run build
      - run: python sessions/eod.py
        env:
          ANTHROPIC_API_KEY: ${{ secrets.ANTHROPIC_API_KEY_C }}
          ALPACA_API_KEY_ID: ${{ secrets.ALPACA_API_KEY_ID_C }}
          ALPACA_API_SECRET_KEY: ${{ secrets.ALPACA_API_SECRET_KEY_C }}
          SUPABASE_URL: ${{ secrets.SUPABASE_URL_C }}
          SUPABASE_KEY: ${{ secrets.SUPABASE_KEY_C }}
          GMAIL_USER: ${{ secrets.GMAIL_USER }}
          GMAIL_APP_PASSWORD: ${{ secrets.GMAIL_APP_PASSWORD }}
```

Node is installed in the EOD workflow because `run_learning_agent` may call
TypeScript agents (e.g., News Analyst extension) in future phases. Installing
it from the start avoids workflow changes later.

---

## Error Handling

| Scenario | Behavior |
|---|---|
| No open positions | Skip force-close step. Proceed to reconcile. |
| Alpaca order submission fails | Retry once after 30s. If still fails: log error, mark position "eod_fail", alert. |
| Fill wait exceeds 10 min | Mark as "eod_timeout". Alert. Reconcile step will catch it next time. |
| Reconcile finds orphan | Alert immediately. Do not attempt auto-close — human review required. |
| Learning Agent fails | Log error to c_traces. Skip learnings. EOD still completes. |
| Protection tier 5 triggered | Set suspended=True in c_agent_config. All subsequent sessions exit at guard check. |

---

## Trace Entries Generated

| step_type | description | outcome |
|---|---|---|
| decision | Force-close result | "forced_N_positions" or "nothing_open" |
| decision | Reconcile result | "clean" or "corrected_N_discrepancies" |
| decision | Protection check | "no_event" or "tier_N_triggered" |
| decision | Goals updated | "updated" |
| decision | Learning Agent result | "completed" or "skipped_no_trades" |
| decision | Session finalized | "eod_complete" |
