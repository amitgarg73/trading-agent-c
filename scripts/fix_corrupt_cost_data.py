"""
One-time cleanup: zero out cost fields on sessions where cost_breakdown is NULL
but total_cost_usd > 0.

These sessions were written by an old version of close_session() that used GPT-4
per-1K pricing ($0.04/1k input, $0.09/1k output) instead of Claude per-M pricing.
The telltale value: 45K tokens in / 2K tokens out → $1.98 exactly.
cost_breakdown was added later, so the old code never wrote it — hence NULL.

Since we cannot reconstruct per-agent breakdowns from those sessions, we zero the
cost fields rather than leave physically-impossible values in the DB.

Usage:
    python3 scripts/fix_corrupt_cost_data.py             # preview only (dry-run)
    python3 scripts/fix_corrupt_cost_data.py --apply     # write to DB
"""
from __future__ import annotations

import argparse
import os
import sys

_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _root)

try:
    import tomllib as _tl
except ImportError:
    import tomli as _tl  # type: ignore[no-redef]

_secrets_path = os.path.join(_root, "dashboard", ".streamlit", "secrets.toml")
if not os.path.exists(_secrets_path):
    # Shared Supabase project — fall back to ai-agent-rca secrets
    _secrets_path = os.path.join(os.path.dirname(_root), "ai-agent-rca", "dashboard", ".streamlit", "secrets.toml")
with open(_secrets_path, "rb") as _f:
    _sec = _tl.load(_f)

from supabase import create_client
db = create_client(_sec["SUPABASE_URL"], _sec["SUPABASE_KEY"])


_GPT4_INPUT_COST_PER_1K  = 0.04  # GPT-4-turbo pricing that caused the bug
_GPT4_OUTPUT_COST_PER_1K = 0.09

def _is_known_corrupt(r: dict) -> bool:
    """
    Returns True if the cost is provably wrong: matches the old GPT-4 per-1K formula
    to within $0.001. Sessions where close_session() was called with this old formula
    produce cost_usd = (tokens_in / 1000 * 0.04) + (tokens_out / 1000 * 0.09).
    """
    cost = r.get("total_cost_usd") or 0.0
    tin  = r.get("total_tokens_input")  or 0
    tout = r.get("total_tokens_output") or 0
    if tin == 0:
        return False
    expected = (tin / 1000 * _GPT4_INPUT_COST_PER_1K) + (tout / 1000 * _GPT4_OUTPUT_COST_PER_1K)
    return abs(cost - expected) < 0.001


def find_null_breakdown_sessions() -> list[dict]:
    """Sessions where cost_breakdown is NULL but total_cost_usd > 0."""
    rows = db.table("c_sessions").select(
        "id,date,total_cost_usd,total_tokens_input,total_tokens_output,cost_breakdown"
    ).execute().data or []
    return [
        r for r in rows
        if r.get("cost_breakdown") is None and (r.get("total_cost_usd") or 0) > 0
    ]


def main(apply: bool) -> None:
    sessions = find_null_breakdown_sessions()
    if not sessions:
        print("No sessions with NULL cost_breakdown and cost > 0 found.")
        return

    known_corrupt = [r for r in sessions if _is_known_corrupt(r)]
    possibly_real = [r for r in sessions if not _is_known_corrupt(r)]

    header = f"  {'Session ID':<38} {'Date':<12} {'Cost':>9} {'Tokens In':>10} {'Tokens Out':>10}"
    divider = "  " + "-" * 85

    def _fmt(r: dict) -> str:
        return (f"  {r['id']:<38} {r['date']:<12} "
                f"${r.get('total_cost_usd', 0):>8.4f} "
                f"{r.get('total_tokens_input', 0):>10,} "
                f"{r.get('total_tokens_output', 0):>10,}")

    print(f"\n{'[DRY-RUN] ' if not apply else ''}DEFINITELY CORRUPT ({len(known_corrupt)} sessions)")
    print("  Old GPT-4 per-1K pricing formula: cost = (in/1k)×$0.04 + (out/1k)×$0.09")
    print(header)
    print(divider)
    for r in known_corrupt:
        print(_fmt(r))

    print(f"\n{'[DRY-RUN] ' if not apply else ''}POSSIBLY REAL — missing cost_breakdown only ({len(possibly_real)} sessions)")
    print("  Cost values look reasonable; old close_session() didn't write cost_breakdown.")
    print("  These are NOT zeroed. Review manually if needed.")
    print(header)
    print(divider)
    for r in possibly_real:
        print(_fmt(r))

    if not apply:
        print(f"\nRun with --apply to zero the {len(known_corrupt)} definitely-corrupt session(s).")
        return

    if not known_corrupt:
        print("\nNothing to apply.")
        return

    for r in known_corrupt:
        db.table("c_sessions").update({
            "total_cost_usd":      0.0,
            "total_tokens_input":  0,
            "total_tokens_output": 0,
        }).eq("id", r["id"]).execute()

    print(f"\nZeroed cost fields on {len(known_corrupt)} session(s).")
    print("cost_breakdown stays NULL — no per-agent breakdown was captured for these sessions.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true", help="Write zeros to DB (default: dry-run)")
    args = parser.parse_args()
    main(apply=args.apply)
