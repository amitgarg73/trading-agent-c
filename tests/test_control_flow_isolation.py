"""
Trading decisions must not depend on the observability platform.

Trading Agent C models a real customer: its own environment, its own database, telemetry sent
outward to Provy. Telemetry flowing out is correct and expected. Reading back in is not -- a
customer cannot have their trading stop because their monitoring vendor changed something.

That is not hypothetical here. Between 2026-07-25 and 2026-07-27 every session died: Provy split
its databases, trace data began landing in Provy production while the agent kept reading the
pre-production project, and premarket created runs that the very next lookup could not find.
Three days of trading lost to a change in a monitoring system.

These tests fail if any control-flow module starts reading Provy's tables again.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent

# Modules that decide whether and what to trade. Scripts, dashboards, eval and analysis tools are
# excluded on purpose: they are allowed to read Provy, because nothing they do places an order.
CONTROL_FLOW_DIRS = ("sessions", "core", "agents", "scanner")

# Provy's tables. Reached through the shared Supabase client, these are cross-system reads.
PROVY_TABLES = ("ag_sessions", "ag_traces", "ag_evals", "ag_outcomes")

_TABLE_CALL = re.compile(r"""\.table\(\s*["'](ag_\w+)["']""")


def _control_flow_files() -> list[Path]:
    files: list[Path] = []
    for d in CONTROL_FLOW_DIRS:
        files.extend(sorted((REPO / d).rglob("*.py")))
    return files


def test_control_flow_files_exist():
    """Guard the guard: a typo'd directory would make every test below vacuously pass."""
    assert len(_control_flow_files()) > 10


@pytest.mark.parametrize("path", _control_flow_files(), ids=lambda p: str(p.relative_to(REPO)))
def test_no_provy_table_reads_in_control_flow(path: Path):
    hits = _TABLE_CALL.findall(path.read_text())
    assert not hits, (
        f"{path.relative_to(REPO)} queries Provy's {', '.join(sorted(set(hits)))}. "
        "Trading decisions must read the agent's own run record (core.run_state) instead -- "
        "this exact coupling cost three days of trading in July 2026."
    )


def test_run_state_is_the_documented_home_for_run_questions():
    """The replacement has to actually exist and answer the questions the old reads answered."""
    from core import run_state

    for fn in (
        "open_run",
        "close_run",
        "today_premarket_run_id",
        "today_premarket_run",
        "read_run",
        "get_pending_trades",
        "set_pending_trades",
        "clear_pending_trades",
        "stamp_entry_scan",
        "last_entry_scan_at",
    ):
        assert callable(getattr(run_state, fn)), f"core.run_state.{fn} is missing"


def test_run_state_reads_only_the_agents_own_table():
    source = (REPO / "core" / "run_state.py").read_text()
    assert not _TABLE_CALL.findall(source)
    assert '_TABLE = "c_sessions"' in source
