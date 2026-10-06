"""
Broker-versus-database holdings report (argus#1567). READ-ONLY and ADDITIVE.

It compares what the broker holds with the open rows in c_positions and says what differs. It
never places an order and never writes to c_positions; its only outputs are text for the EOD
alert and a detail dict for a trace decision. What to DO about a difference (close an orphan,
correct a row) stays a deliberate human decision.

It exists because three August holdings (CL, JPM, PSX) sat at the broker for weeks with no open
database row: EOD only looked at the database rows, so nothing ever compared the two sides.

A broker that cannot be read is reported as exactly that. It is never reported as a pass.
"""
from __future__ import annotations

from typing import Optional

_QTY_TOLERANCE = 1e-6


def compare_holdings(open_rows: list[dict], holdings: Optional[dict[str, float]]) -> dict:
    """
    open_rows: c_positions rows with status open (ticker, shares).
    holdings:  {symbol: quantity} from the broker, or None when the broker could not be read.
    """
    if holdings is None:
        return {"status": "broker_unreadable", "ok": False, "orphans_at_broker": [],
                "rows_without_holding": [], "quantity_mismatches": []}

    db_qty: dict[str, int] = {}
    for r in open_rows:
        t = str(r.get("ticker") or "")
        if t:
            db_qty[t] = db_qty.get(t, 0) + int(r.get("shares") or 0)

    orphans = [{"ticker": t, "broker_qty": holdings[t]}
               for t in sorted(holdings) if t not in db_qty and abs(holdings[t]) > _QTY_TOLERANCE]
    no_holding = [{"ticker": t, "db_shares": db_qty[t]}
                  for t in sorted(db_qty) if abs(holdings.get(t, 0.0)) <= _QTY_TOLERANCE]
    mismatches = [{"ticker": t, "db_shares": db_qty[t], "broker_qty": holdings[t]}
                  for t in sorted(db_qty)
                  if t in holdings and abs(holdings[t]) > _QTY_TOLERANCE
                  and abs(holdings[t] - db_qty[t]) > _QTY_TOLERANCE]
    ok = not (orphans or no_holding or mismatches)
    return {"status": "match" if ok else "differences", "ok": ok,
            "orphans_at_broker": orphans, "rows_without_holding": no_holding,
            "quantity_mismatches": mismatches}


def format_report(report: dict) -> str:
    """Plain-language text for the alert body."""
    head = "Broker reconciliation (read-only, nothing was changed):"
    if report["status"] == "broker_unreadable":
        return f"{head} could not read the broker, so nothing was compared. This is not a pass."
    if report["status"] == "report_failed":
        return (f"{head} could not be produced ({report.get('error', 'unknown error')}). "
                f"This is not a pass.")
    if report["ok"]:
        return f"{head} broker holdings match the open database positions."
    lines = [head]
    for o in report["orphans_at_broker"]:
        lines.append(f"  - {o['ticker']}: broker holds {o['broker_qty']:g} but there is no open "
                     f"database row (nothing will close it automatically unless EOD selects it).")
    for r in report["rows_without_holding"]:
        lines.append(f"  - {r['ticker']}: database row open for {r['db_shares']} share(s) but the "
                     f"broker holds none.")
    for m in report["quantity_mismatches"]:
        lines.append(f"  - {m['ticker']}: database says {m['db_shares']}, broker holds "
                     f"{m['broker_qty']:g}.")
    return "\n".join(lines)


def run_report() -> dict:
    """Read both sides and compare. Never raises: a failure becomes a 'report_failed' result."""
    try:
        from core import alpaca
        from core.db import get_client
        holdings = alpaca.get_broker_holdings()
        rows = (
            get_client().table("c_positions").select("ticker,shares")
            .eq("status", "open").execute().data
        ) or []
        return compare_holdings(rows, holdings)
    except Exception as e:
        return {"status": "report_failed", "ok": False, "error": str(e)[:200],
                "orphans_at_broker": [], "rows_without_holding": [], "quantity_mismatches": []}
