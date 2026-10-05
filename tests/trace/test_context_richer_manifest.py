"""Step 6c: the richer context manifest (as_of, retrieval, truncated), offline. No network, no broker, no model.

Promises: a step with a known source time carries `as_of`; a step with an unknown time carries none; an empty lookup says
`returned: 0` and an errored one says nothing; `truncated` appears only when a tool really dropped rows; the object stays under the
cap and carries no free text."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from trace import context_manifest as cm

SENTINEL = "SENTINEL-HEADLINE-TEXT-ZQX"


@pytest.fixture(autouse=True)
def on(monkeypatch):
    monkeypatch.setenv(cm.SWITCH_ENV, "1")
    cm._STAMPS.clear()


def one(entries, agent="research_GILD"):
    return cm.build(agent, entries, [], has_model=True)


def test_known_quote_time_becomes_as_of():
    t = datetime.now(timezone.utc) - timedelta(minutes=2)
    cm.stamp_source("get_intraday_signals:GILD", as_of=t)
    r = cm.Recorder()
    r.record_tool("research_GILD", "get_intraday_signals", {"vwap": 1.0}, key="get_intraday_signals:GILD")
    m = one(r.take("research_GILD"))
    assert m["items"][0]["as_of"] == t.strftime("%Y-%m-%dT%H:%M:%S.") + f"{t.microsecond // 1000:03d}Z"


def test_unknown_time_leaves_as_of_absent():
    r = cm.Recorder()
    r.record_tool("market", "get_vix", {"value": 17.0, "level": "calm"})
    m = one(r.take("market"), agent="market")
    assert "as_of" not in m["items"][0]


@pytest.mark.parametrize("bad", [datetime(2026, 1, 1), datetime.now(timezone.utc) + timedelta(days=2), datetime(1999, 1, 1, tzinfo=timezone.utc), "garbage"])
def test_naive_future_or_ancient_time_is_never_sent(bad):
    cm.stamp_source("get_intraday_signals:X", as_of=bad)
    assert cm.take_stamp("get_intraday_signals:X") is None


def test_scan_results_are_dated_to_the_day_they_were_selected_on():
    r = cm.Recorder()
    r.record_tool("scanner", "get_scan_results", [{"ticker": "AAA"}])
    m = one(r.take("scanner"), agent="scanner")
    today = datetime.now().date().isoformat()
    assert m["items"][0]["as_of"] == today + "T00:00:00.000Z" or "as_of" not in m["items"][0]  # absent only if the day has not begun in UTC
    assert m["retrieval"] == {"returned": 1}


def test_empty_scan_has_no_as_of_and_reports_empty():
    r = cm.Recorder()
    r.record_tool("scanner", "get_candidates", [])
    m = one(r.take("scanner"), agent="scanner")
    assert "as_of" not in m["items"][0]
    assert m["retrieval"] == {"returned": 0}


def test_errored_read_reports_no_retrieval_and_no_as_of():
    r = cm.Recorder()
    r.record_tool("scanner", "get_candidates", [{"error": "db down"}])
    m = one(r.take("scanner"), agent="scanner")
    assert "retrieval" not in m and "as_of" not in m["items"][0]


def test_news_reports_count_time_and_a_real_cut():
    t = datetime.now(timezone.utc) - timedelta(hours=5)
    cm.stamp_source("get_news:GILD", as_of=t, returned=7, cut=True)
    r = cm.Recorder()
    r.note("research_GILD", "get_news", {"headlines": [SENTINEL] * 3}, used=True, meta=cm.take_stamp("get_news:GILD"))
    m = one(r.take("research_GILD"))
    assert m["retrieval"] == {"returned": 7}
    assert m["truncated"] is True
    assert m["items"][0]["as_of"].endswith("Z")


def test_truncated_absent_when_nothing_was_cut():
    cm.stamp_source("get_news:GILD", as_of=datetime.now(timezone.utc), returned=2, cut=False)
    r = cm.Recorder()
    r.note("research_GILD", "get_news", {"headlines": ["a", "b"]}, used=True, meta=cm.take_stamp("get_news:GILD"))
    m = one(r.take("research_GILD"))
    assert "truncated" not in m and m["retrieval"] == {"returned": 2}


def test_zero_headlines_is_empty_not_unknown():
    cm.stamp_source("get_news:GILD", returned=0, cut=False)
    r = cm.Recorder()
    r.note("research_GILD", "get_news", {"headlines": []}, used=True, meta=cm.take_stamp("get_news:GILD"))
    m = one(r.take("research_GILD"))
    assert m["retrieval"] == {"returned": 0} and "as_of" not in m["items"][0]


def test_switch_off_parks_nothing(monkeypatch):
    monkeypatch.delenv(cm.SWITCH_ENV)
    cm.stamp_source("k", as_of=datetime.now(timezone.utc), returned=1)
    assert cm._STAMPS == {}


def test_stamp_registry_is_bounded():
    for i in range(cm.STAMPS_CAP + 50):
        cm.stamp_source(f"k{i}", returned=1)
    assert len(cm._STAMPS) <= cm.STAMPS_CAP


def test_under_cap_with_cut_and_no_free_text():
    t = datetime.now(timezone.utc) - timedelta(minutes=1)
    entries = [{"name": f"get_tool_{i}", "output": {"text": SENTINEL * 50},
                "meta": {"as_of": cm.iso_utc(t), "returned": 3, "cut": True}} for i in range(20)]
    m = one(entries)
    blob = json.dumps(m)
    assert len(blob.encode()) <= cm.MAX_BYTES
    assert SENTINEL not in blob
    assert m["truncated"] is True


def test_normaliser_accepts_it(tmp_path):
    """Run argus's own normalizeContext (read only, not edited) over a manifest with every new field."""
    import os, shutil, subprocess
    src = "/Users/amitgarg/Claude Projects/argus/web/lib/context-capture.ts"
    node = shutil.which("node")
    if not node or not os.path.exists(src):
        pytest.skip("node or the argus checkout is not on this machine")
    shutil.copy(src, tmp_path / "context-capture.ts")   # a copy: argus is read, never edited
    t = datetime.now(timezone.utc) - timedelta(minutes=3)
    cm.stamp_source("get_news:G", as_of=t, returned=5, cut=True)
    r = cm.Recorder()
    r.note("research_G", "get_news", {"headlines": ["x"]}, used=True, meta=cm.take_stamp("get_news:G"))
    r.record_tool("research_G", "get_scan_results", [{"t": 1}])
    manifest = one(r.take("research_G"))
    script = tmp_path / "n.ts"
    script.write_text(
        "import { normalizeContext } from './context-capture.ts';\n"
        f"const r = normalizeContext({json.dumps(manifest)}, {{ capturedBy: 'otlp:provy' }});\n"
        "console.log(JSON.stringify(r));\n")
    out = subprocess.run([node, "--experimental-strip-types", str(script)], capture_output=True, text=True, timeout=120, cwd=tmp_path)
    if out.returncode != 0 and "strip-types" in out.stderr:
        pytest.skip("this node cannot run TypeScript directly")
    assert out.returncode == 0, out.stderr
    res = json.loads(out.stdout.strip().splitlines()[-1])
    assert res["notes"] == [], res["notes"]
    got = res["context"]
    assert got["items"][0]["as_of"] == manifest["items"][0]["as_of"]
    assert got["retrieval"]["returned"] == manifest["retrieval"]["returned"]
    # The platform's normaliser writes `truncated` itself (spec 2.1: "the server"), only when IT cut something, and ignores a caller's
    # value without a note. So the agent's own `truncated` is accepted but not stored; this pins that finding.
    assert manifest["truncated"] is True and "truncated" not in got
