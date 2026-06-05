"""
Manual premarket validation script.

Bypasses time-window and trading-day guards so you can run the full premarket
pipeline on demand. Tags the session as is_simulated=True so it does not block
the real premarket run and is excluded from live dashboards.

Usage:
    python3 scripts/run_premarket_now.py
    python3 scripts/run_premarket_now.py --skip-scanner   # if c_scan_results already populated
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from uuid import uuid4

# ── Args ─────────────────────────────────────────────────────────────────────

parser = argparse.ArgumentParser()
parser.add_argument("--skip-scanner", action="store_true",
                    help="Skip run_scanner() — use whatever is already in c_scan_results")
args = parser.parse_args()

# ── Imports ───────────────────────────────────────────────────────────────────

from agents.orchestrator import run_premarket_pipeline
from core.db import get_client
from core.params import load_params
from trace.logger import TraceLogger

# ── Scanner ───────────────────────────────────────────────────────────────────

if not args.skip_scanner:
    print("[validate] Running scanner to populate c_scan_results...")
    from scanner.scanner import run_scanner
    n = run_scanner(scan_date=date.today())
    print(f"[validate] Scanner: {n} candidates written to c_scan_results")
    if n == 0:
        print("[validate] 0 scanner candidates — Scanner Agent will rely on gap-ups only.")
else:
    print("[validate] --skip-scanner set; using existing c_scan_results rows.")

# ── Session setup ─────────────────────────────────────────────────────────────

session_id = str(uuid4())
params     = load_params()
tracer     = TraceLogger(session_id, session_type="premarket")

# Tag as simulated so the session guard and live dashboards ignore it
get_client().table("ag_sessions").update(
    {"is_simulated": True}
).eq("id", session_id).execute()

print(f"\n[validate] Session {session_id} (is_simulated=True)")
print("[validate] Running premarket pipeline...\n")

# ── Pipeline ──────────────────────────────────────────────────────────────────

try:
    result   = run_premarket_pipeline(tracer, params)
    v2_rep   = result.pop("_v2_market_report", {})
    trades   = result.get("trades", [])
    terminal = result["session_meta"]["terminal_reason"]
    meta     = result["session_meta"]

    tracer.close_session(
        terminal_reason=terminal,
        trades_proposed=len(result.get("trades", [])),
        trades_approved=len(trades),
        trades_executed=0,           # never execute in validation run
        retry_triggered=meta.get("retry_triggered", False),
    )

    # Pull scanner decision row from c_traces
    _sc_rows = (
        get_client()
        .table("ag_traces")
        .select("tool_output")
        .eq("session_id", session_id)
        .eq("agent", "scanner")
        .eq("step_type", "decision")
        .execute()
        .data or []
    )
    scanner_detail = {}
    if _sc_rows:
        raw = _sc_rows[0].get("tool_output") or {}
        scanner_detail = raw if isinstance(raw, dict) else (json.loads(raw) if isinstance(raw, str) else {})

    print("\n" + "=" * 60)
    print(f"  Terminal:  {terminal}")
    print(f"  Trades:    {len(trades)}")
    if trades:
        for t in trades:
            print(f"    {t['ticker']:6s}  entry=${t['entry_price']:.2f}  "
                  f"target=${t['target_price']:.2f}  stop=${t['stop_loss']:.2f}  "
                  f"size=${t['position_size']:.0f}")
    print(f"  Retry:     {meta.get('retry_triggered', False)}")
    print(f"  Market:    {v2_rep.get('decision', '?')} / VIX {v2_rep.get('vix_value', '?')}")
    print(f"  Session:   {session_id[:8]} (simulated — not executed)")

    if scanner_detail:
        print(f"\n  Scanner:")
        print(f"    candidates: {scanner_detail.get('n_returned', '?')}")
        print(f"    regime:     {scanner_detail.get('regime', '?')}")
        print(f"    dropped:    {scanner_detail.get('dropped_count', '?')}")
        print(f"    rationale:  {scanner_detail.get('scan_rationale', '?')}")
    print("=" * 60)

except Exception as e:
    tracer.close_session(terminal_reason="error")
    print(f"\n[validate] Pipeline error: {e}", file=sys.stderr)
    raise
