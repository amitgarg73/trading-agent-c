"""
The agent reports to Provy over Provy's API and never writes Provy's tables.

The shared Supabase client (`core.db.get_client`) points at the project that holds this fleet's own
record (the `c_*` tables). That project is ALSO Provy's pre-production database, so a write to an
`ag_*` table through it lands in the wrong Provy environment. `write_eod_outcome_metrics` did exactly
that: after Provy split its databases on 2026-07-25 this fleet's sessions lived only in Provy
production, so every EOD insert into `ag_outcomes` failed on its session foreign key, was caught,
printed, and the run stayed green, from July to 13 Sep 2026. The per-ticker P&L added on 16 Aug never
landed anywhere.

Scripts are excluded: they are one-off operator tools, not the running agent.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
RUNTIME_DIRS = ("sessions", "core", "agents", "scanner", "evals", "trace")

# `.table("ag_x")` followed, within the same call chain, by a write verb.
_WRITE = re.compile(r"""\.table\(\s*["'](ag_\w+)["']\s*\)(?:(?!\.table\()[\s\S]){0,400}?\.(insert|upsert|update|delete)\(""")


def _runtime_files() -> list[Path]:
    out: list[Path] = []
    for d in RUNTIME_DIRS:
        out.extend(sorted((REPO / d).rglob("*.py")))
    return out


def test_runtime_files_exist():
    """Guard the guard: a renamed directory would make the test below pass vacuously."""
    assert len(_runtime_files()) > 10


@pytest.mark.parametrize("path", _runtime_files(), ids=lambda p: str(p.relative_to(REPO)))
def test_no_writes_to_provy_tables(path: Path):
    hits = [f"{m.group(1)}.{m.group(2)}" for m in _WRITE.finditer(path.read_text())]
    assert not hits, (
        f"{path.relative_to(REPO)} writes Provy's {', '.join(sorted(set(hits)))} through the shared "
        "client, which lands in Provy pre-production. Report through Provy's ingest API instead."
    )


def test_the_guard_catches_the_write_it_was_written_for():
    """Mutation check: the exact shape of the removed insert must be flagged."""
    removed = 'client.table("ag_outcomes").insert(rows).execute()'
    assert _WRITE.search(removed)
    assert not _WRITE.search('client.table("c_daily_performance").upsert(row).execute()')
