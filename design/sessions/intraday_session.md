# Intraday Session — Design Doc

**File:** `sessions/intraday.py`
**Type:** Python service (not an agent)
**Runs:** Every 15 minutes, Mon-Fri 9:15 AM to 3:50 PM ET via GitHub Actions
**Purpose:** Sync positions with Alpaca, record closed trades, enforce goal gates,
and optionally trigger Research Agent for new entries when capacity is available.

---

## Design Principle

The intraday session is a polling service, not a decision-making agent. Its primary
job is position hygiene: detecting bracket exits that happened between polls and
recording them. Research Agent runs at most once per hour (enforced by
`intraday_entry_min_interval_mins = 55`) to scan for additional opportunities, but
only before 1:00 PM ET.

Most 15-min polls cost $0 in API calls — only the hourly scan polls Claude.

---

## Trading Day Configuration

```python
# config/settings.py

TRADING_DAYS = ["MON", "TUE", "WED", "THU", "FRI"]
INTRADAY_POLL_START_ET = time(9, 15)   # First poll before market open
INTRADAY_POLL_END_ET   = time(15, 50)  # Last poll before EOD
NEW_ENTRY_WINDOW_END   = time(13, 0)   # No new entries after 1:00 PM ET
FORCE_CLOSE_TIME_ET    = time(15, 45)  # Force-close signal (handled by EOD session)
```

---

## Session Flow

```
Every 15 min GitHub Actions fires strategy_c_intraday.yml

1. CHECK TRADING DAY
   If today is not in TRADING_DAYS: exit immediately.
   If current time ET is outside [POLL_START, POLL_END]: exit.

2. LOAD STATE
   session_id = db.get_today_session_id()
   If no session_id: log warning + exit. (Premarket did not run today.)
   config = load_agent_config()
   params = load_strategy_params()

3. PRINCIPAL PROTECTION CHECK
   protection = check_protection_status()
   If protection.suspended:
     log_status("suspended", protection.reason)
     send_alert_if_new(protection)
     exit.

4. SYNC POSITIONS WITH ALPACA
   alpaca_positions = alpaca.get_positions()
   db_positions = db.get_open_positions(session_id)
   newly_closed = detect_closed(alpaca_positions, db_positions)

5. RECORD CLOSED TRADES
   For each closed position:
     fill_details = alpaca.get_order_fills(position.order_id)
     record_trade_exit(
       ticker=position.ticker,
       exit_price=fill_details.avg_fill_price,
       exit_time=fill_details.filled_at,
       exit_reason=classify_exit(fill_details),  # target / stop / manual
       session_id=session_id,
     )
     db.update_position_status(position, "closed")

6. UPDATE GOAL PROGRESS
   daily_pnl = sum(t.realized_pnl for t in db.get_today_closed_trades())
   goal_status = evaluate_goals(daily_pnl)
   db.update_goal_current_values(daily_pnl)

7. GOAL GATE CHECK
   If goal_status.lock_in_mode:
     log_status("lock_in_mode", daily_pnl=daily_pnl, target=goal_status.daily_target)
     exit.  (No new entries. Let bracket orders run.)

   If goal_status.pnl_floor_hit:
     log_status("pnl_floor_hit", daily_pnl=daily_pnl)
     exit.  (Extra caution gate hit. No new entries today.)

8. NEW ENTRY CHECK (conditional)
   If NOT config.get("enable_intraday_entries", True):
     log_status("normal_no_new_entries", daily_pnl=daily_pnl)
     exit.

   min_interval = config.get("intraday_entry_min_interval_mins", 55)
   last_scan = get_last_entry_scan_time(session_id)  # queries c_traces
   If last_scan is not None AND (now_utc - last_scan).minutes < min_interval:
     log_status("entry_scan_too_recent", elapsed_mins=..., min_interval=...)
     exit.  (Hourly cadence gate — Research Agent ran less than 55 min ago.)

   If current_time_et.time() >= NEW_ENTRY_WINDOW_END:
     log_status("past_entry_window", current_time=current_time_et)
     exit.

   available_slots = params.max_positions - db.count_open_positions()
   If available_slots <= 0:
     log_status("no_capacity", open=db.count_open_positions())
     exit.

   market_report = db.get_today_market_report(session_id)
   If market_report.decision != "GO":
     log_status("market_not_go", signal=market_report.decision)
     exit.

9. INTRADAY RESEARCH PASS
   proposals = run_research_agent_light(
     session_id=session_id,
     context="intraday_scan",
     max_new_tickers=min(2, available_slots),
     max_tool_calls=10,
     exclude_tickers=db.get_today_investigated_tickers(session_id),
   )
   If not proposals or proposals.proposals == []:
     log_status("no_intraday_candidates")
     exit.

10. RISK AGENT (same constraints as premarket)
    verdicts = run_risk_agent(session_id, proposals, tracer)
    approved = [v for v in verdicts.verdicts if v.verdict == "APPROVED"]
    If not approved:
      log_status("intraday_all_rejected")
      exit.

11. PLACE INTRADAY ORDERS
    For each approved trade:
      order = alpaca.place_bracket_order(trade)
      db.insert_position(trade, order, session_id, entry_context="intraday")
    log_status("intraday_entries_placed", count=len(approved))
```

---

## Exit Reason Classification

When Alpaca reports a position closed, the exit reason is inferred from which bracket leg filled:

```python
def classify_exit(fill_details: dict) -> str:
    order_type = fill_details.get("order_type")
    side = fill_details.get("side")

    if order_type == "limit" and side == "sell":
        return "target"
    if order_type == "stop" and side == "sell":
        return "stop"
    if order_type == "market" and side == "sell":
        return "eod_forced"
    return "manual"
```

---

## Lock-in Mode

Lock-in mode activates when `daily_pnl >= daily_pnl_target` from c_goals.

In lock-in mode:
- No new position entries
- Existing bracket orders remain in place (not force-closed)
- All subsequent polls skip the new entry check
- Status logged each poll: `"lock_in_mode"` with current P&L

The daily target is configurable per `c_goals.goal_type = "daily_pnl_target"`.
If no goal is set, lock-in mode never activates.

---

## Research Agent Light Pass

When intraday entries are enabled, Research Agent runs with a reduced budget:

```python
def run_research_agent_light(
    session_id: str,
    context: str,
    max_new_tickers: int,
    max_tool_calls: int,
    exclude_tickers: list[str],
) -> dict:
    """
    Runs Research Agent with a tighter budget for intraday scans.
    - Max 2 new tickers
    - Max 10 tool calls (vs 25 in premarket)
    - Excludes tickers already investigated today
    - Same tool set, same Risk Agent afterwards
    """
    intraday_context = (
        f"This is an intraday scan at {datetime.now(ET).strftime('%H:%M')} ET. "
        f"Look for 1-2 momentum plays only. Do not revisit: {', '.join(exclude_tickers)}. "
        f"Use a maximum of {max_tool_calls} tool calls total. "
        f"Only propose tickers with score >= {params.strategy_min_score + 1} "
        f"(higher bar for intraday entries)."
    )
    return run_research_agent(
        session_id=session_id,
        market_report=db.get_today_market_report(session_id),
        tracer=tracer,
        additional_context=intraday_context,
        max_tool_calls=max_tool_calls,
    )
```

Intraday entries require score at least 1 point above the premarket threshold.
This avoids chasing weaker setups when the best candidates have already been evaluated.

---

## c_agent_config Keys (intraday-relevant)

| config_key | default | description |
|---|---|---|
| `enable_intraday_entries` | true | Master toggle for new intraday entries |
| `intraday_entry_min_interval_mins` | 55 | Min minutes between entry scans — enforces hourly cadence |
| `intraday_min_score_bonus` | 1 | Extra score required above strategy_min_score |
| `intraday_max_new_positions` | 2 | Cap on new positions per scan |
| `intraday_entry_window_end` | "13:00" | No new entries after this time (ET) |
| `trading_days` | ["MON","TUE","WED","THU","FRI"] | Which days to run |

---

## Intraday Trace Entries

Intraday session writes to c_traces for each poll:

| step_type | description | outcome |
|---|---|---|
| decision | Poll status (no agent calls) | "lock_in_mode" / "normal" / "suspended" / "no_capacity" / "past_entry_window" |
| tool_call | (if entries attempted) Research Agent calls | standard research outcomes |
| decision | Intraday entry result | "entries_placed" / "all_rejected" / "no_candidates" |

Each poll is grouped under the same `session_id` as the premarket run. The `sequence` field continues from the premarket trace — intraday events are a continuation of the same session.

---

## GitHub Actions Workflow (strategy_c_intraday.yml)

```yaml
name: Strategy C — Intraday
on:
  schedule:
    # Every 15 minutes, Mon-Fri 9:15 AM to 3:50 PM ET (14:15-20:50 UTC)
    - cron: "15,30,45,0 14 * * 1-5"   # 9:15-9:45 ET
    - cron: "0,15,30,45 15-19 * * 1-5" # 10:00-3:45 ET
    - cron: "0,15,30,45,50 20 * * 1-5" # 4:00-4:50 ET (safety window)

jobs:
  intraday:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"
      - run: pip install -r requirements.txt
      - run: python sessions/intraday.py
        env:
          ANTHROPIC_API_KEY: ${{ secrets.ANTHROPIC_API_KEY_C }}
          ALPACA_API_KEY_ID: ${{ secrets.ALPACA_API_KEY_ID_C }}
          ALPACA_API_SECRET_KEY: ${{ secrets.ALPACA_API_SECRET_KEY_C }}
          SUPABASE_URL: ${{ secrets.SUPABASE_URL_C }}
          SUPABASE_KEY: ${{ secrets.SUPABASE_KEY_C }}
```

All secrets use `_C` suffix to confirm they are Strategy C credentials, not shared with A or B.

---

## Isolation from Strategies A and B

- Separate GitHub repo — no shared workflow files
- Separate Alpaca credentials (`_C` suffix)
- Separate Supabase project (different URL)
- Strategy C order prefix: `stratc_` (never collides with A's `strat_` or B's `stratb_`)
- No imports from trading-agent/ or trading-agent-b/

Any failure in Strategy C's intraday session has zero impact on A or B.
