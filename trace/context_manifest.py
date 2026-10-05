"""The context manifest for a decision step (argus#1519, platform spec argus#1505).

WHAT IT IS. A small label on the span of a step that asked a model to decide: what that step was GIVEN (which tool results, which
learned memory, which upstream steps' output), under which instruction (a version and a fingerprint), and how many things a store
returned. It holds names, fingerprints and counts. It holds NO prompt text, NO model reply, NO tool output and NO ticker.

⛔ BEST EFFORT, AND INERT BY DEFAULT. Nothing here runs unless the switch PROVY_CONTEXT_MANIFEST is truthy. Every public entry point
catches everything and answers None or does nothing, because a trading run must never fail, change a decision, change a prompt or
wait on telemetry (CLAUDE.md: Provy is never in the trade path). There is no new network call, no new file read and no new model
call: the manifest is built from values the step already holds, in memory, in microseconds.

⛔ ABSENT IS UNKNOWN. A field is sent only when this code truly knows it.
  - `as_of` is when the SOURCE was current, never when it was fetched. The only source here that carries a date of its own is the
    learner's memory (the newest entry's date). Everything else is left undated on purpose.
  - `used` is true only where the calling code put the item into the model's prompt by construction (an upstream step's output, the
    news it pre-fetched, the two reads a circuit breaker decided on). A tool result the model chose to ask for is left unmarked:
    whether it shaped the answer is a judgement this code cannot make.
  - 6c (richer manifest): a few tools park what they alone know (the newest headline's own time, the newest 1-minute bar's own
    timestamp, how many headlines the lookup returned, whether they cut the list) via `stamp_source`; the return value they hand the
    model is unchanged. Today's scan rows are dated to their day because the query selects on it. A time is refused unless it carries
    a zone, is after 2000 and is not in the future. `truncated` is sent only when a tool dropped rows. NOTE the platform ignores a
    caller's `truncated` and sets its own when it cuts; ours is accepted and not stored.
  - `retrieval.returned` is sent only for reads that are lookups in a store (the day's scan results and candidates, the recent
    learnings, the news lookup),
    and never for a read that errored, so "returned nothing" and "could not read" stay different.
  - `instruction` is the SHA-256 of the agent's instruction text, taken from the constant the agent itself sends, in memory, and the
    text is never stored. The version is `auto-` plus the first 12 hex digits of that hash, so it changes exactly when the text does.

SOURCE NAMES ARE GENERIC. `tool:<tool name>` for a read, `step:<agent>` for an upstream step's output. A ticker is never in a source
or an id (the per-ticker research agent's name is reduced to its base, `research`).

BOUND. The platform stores at most 4,096 bytes; this stops at 3,900 and drops items from the END of the list first, then says
`truncated`. Order: upstream steps first, then tool results in the order they were read.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import sys
import threading
from datetime import datetime, timezone
from typing import Any, Optional

SWITCH_ENV = "PROVY_CONTEXT_MANIFEST"
ATTRIBUTE = "argus.context"          # the OTLP gateway reads provy.context and, as the legacy spelling, argus.context
MAX_BYTES = 3_900                    # the platform cap is 4,096 on the stored object, which adds `captured_by` and a few keys
MAX_ITEMS = 20                       # the platform keeps the first 20
PENDING_CAP = 40                     # reads remembered per agent between two decision steps; bounds memory if a step never finishes

_TRUTHY = ("1", "true", "yes", "on")

# A read is a tool whose name starts with one of these. A write (`write_learning`, `adjust_param`, `recommend_goal`) is the step's
# OUTPUT, never something it was given, and an unknown prefix is left out rather than guessed at.
_READ_TOOL = re.compile(r"^(get|read|fetch)_[a-z0-9_]+$")
# Learned state the agent keeps for itself, as opposed to a live read of the world.
_MEMORY_TOOLS = frozenset({"read_strategy_params", "read_recent_learnings"})
# Reads that are lookups in a store: the number of rows they returned is the step's retrieval count.
_RETRIEVAL_TOOLS = frozenset({"get_scan_results", "get_candidates", "read_recent_learnings"})
# Reads whose rows are selected by TODAY's date in the query itself (`.eq("date", today)`): every row returned is today's, so the day is
# the rows' own date. Day grain, like the learnings. Never used for an empty or errored read.
_TODAY_KEYED_TOOLS = frozenset({"get_scan_results", "get_candidates"})
# Where each agent's instruction text lives. Looked up in modules the agent has already imported: no import, no file read.
_INSTRUCTION_SOURCES = {
    "market":       ("agents.market_agent",   "_SYSTEM"),
    "scanner":      ("agents.scanner_agent",  "_SELECT_SYSTEM"),
    "research":     ("agents.research_agent", "_INVESTIGATE_SYSTEM"),
    "risk":         ("agents.risk_agent",     "_SYSTEM"),
    "orchestrator": ("agents.orchestrator",   "_SYSTEM"),
    "learner":      ("agents.learning_agent", "_SYSTEM"),
}
_VARIANT_SUFFIX = re.compile(r"^[A-Z]{1,5}$")        # same rule as TraceLogger._base: research_GILD -> research
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}")


# ── Facts only the tool function itself holds ──────────────────────────────────────────────────────────────────────────────────────
# A few tools see a fact they then drop from what they return (the newest headline's own time, a bar's own timestamp, how many rows a
# cap cut). The tool's return value is what the model sees, so it must not change; the fact is parked here under a key (`tool:ticker`)
# and taken exactly once by whoever records the step. Bounded; inert with the switch off; never raises.
_STAMPS: dict = {}
_STAMPS_LOCK = threading.Lock()
STAMPS_CAP = 200
_FLOOR = datetime(2000, 1, 1, tzinfo=timezone.utc)
_SKEW_S = 300


def iso_utc(value: Any) -> Optional[str]:
    """A datetime or ISO 8601 string as `YYYY-MM-DDTHH:MM:SS.mmmZ`, or None. Naive values are refused (a zone is a fact, not a guess),
    as are stamps before 2000 and more than five minutes in the future: the platform would drop those anyway."""
    try:
        if isinstance(value, str):
            value = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        if not isinstance(value, datetime) or value.tzinfo is None:
            return None
        value = value.astimezone(timezone.utc)
        now = datetime.now(timezone.utc)
        if value < _FLOOR or (value - now).total_seconds() > _SKEW_S:
            return None
        return value.strftime("%Y-%m-%dT%H:%M:%S.") + f"{value.microsecond // 1000:03d}Z"
    except Exception:
        return None


def stamp_source(key: str, as_of: Any = None, returned: Optional[int] = None, cut: Optional[bool] = None) -> None:
    """Park what a tool knows about its own read: `as_of` (the source's own time), `returned` (rows the lookup gave back, before any
    cap), `cut` (True only when the tool dropped rows from what it handed on). Absent stays absent. No-op unless the switch is on."""
    try:
        if not enabled():
            return
        meta: dict = {}
        stamped = iso_utc(as_of) if as_of is not None else None
        if stamped:
            meta["as_of"] = stamped
        if isinstance(returned, int) and not isinstance(returned, bool) and returned >= 0:
            meta["returned"] = returned
        if cut is True:
            meta["cut"] = True
        if not meta:
            return
        with _STAMPS_LOCK:
            if len(_STAMPS) >= STAMPS_CAP and key not in _STAMPS:
                _STAMPS.pop(next(iter(_STAMPS)))
            _STAMPS[key] = meta
    except Exception:
        pass


def take_stamp(key: Optional[str]) -> Optional[dict]:
    """The parked facts for `key`, once. None when there are none."""
    try:
        if not key:
            return None
        with _STAMPS_LOCK:
            return _STAMPS.pop(key, None)
    except Exception:
        return None


def enabled() -> bool:
    """The switch. Default OFF. Read on every call so it can be flipped between runs with no deploy."""
    try:
        return os.environ.get(SWITCH_ENV, "").strip().lower() in _TRUTHY
    except Exception:
        return False


def base_agent(agent: str) -> str:
    parts = agent.split("_")
    if len(parts) < 2:
        return agent
    return "_".join(parts[:-1]) if _VARIANT_SUFFIX.match(parts[-1]) else agent


def _finite(value: Any) -> Any:
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, dict):
        return {str(k): _finite(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_finite(v) for v in value]
    return value


def content_hash(value: Any) -> Optional[str]:
    """`sha256:` plus the hex digest of the value as canonical JSON (sorted keys, no spaces). Computed in memory, never stored."""
    try:
        text = json.dumps(_finite(value), sort_keys=True, separators=(",", ":"), default=str, allow_nan=False)
        return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()
    except Exception:
        return None


def instruction_for(agent: str) -> Optional[dict]:
    """`{version, hash}` of the agent's instruction text, or None when the constant cannot be found. The text is hashed and dropped."""
    try:
        spec = _INSTRUCTION_SOURCES.get(base_agent(agent))
        if not spec:
            return None
        module = sys.modules.get(spec[0])
        text = getattr(module, spec[1], None) if module is not None else None
        if not isinstance(text, str) or not text:
            return None
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        return {"version": "auto-" + digest[:12], "hash": "sha256:" + digest}
    except Exception:
        return None


def _is_error(output: Any) -> bool:
    if isinstance(output, dict):
        return "error" in output
    if isinstance(output, list):
        return len(output) == 1 and isinstance(output[0], dict) and "error" in output[0]
    return False


def _newest_date(output: Any) -> Optional[str]:
    """The newest `learning_date` in a list of learnings, as an ISO instant, or None. Day grain: that is all the store keeps."""
    try:
        if not isinstance(output, list):
            return None
        days = [r.get("learning_date") for r in output if isinstance(r, dict)]
        days = [d[:10] for d in days if isinstance(d, str) and _DATE.match(d)]
        if not days:
            return None
        newest = max(days)
        stamp = datetime.strptime(newest, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        if stamp > datetime.now(timezone.utc):
            return None
        return stamp.strftime("%Y-%m-%dT%H:%M:%S.000Z")
    except Exception:
        return None


def _day_start(day: Any) -> Optional[str]:
    """`YYYY-MM-DD` as that day's start in UTC, via iso_utc (so a day that has not begun yet is refused)."""
    try:
        if not isinstance(day, str) or not _DATE.match(day):
            return None
        return iso_utc(datetime.strptime(day[:10], "%Y-%m-%d").replace(tzinfo=timezone.utc))
    except Exception:
        return None


def tool_item(name: str, output: Any, used: Optional[bool] = None, kind: Optional[str] = None,
              meta: Optional[dict] = None, seen_on: Optional[str] = None) -> dict:
    item: dict = {"kind": kind or ("memory" if name in _MEMORY_TOOLS else "tool_result"), "source": f"tool:{name}"}
    as_of = (meta or {}).get("as_of")
    if not as_of and name == "read_recent_learnings":
        as_of = _newest_date(output)
    if not as_of and name in _TODAY_KEYED_TOOLS and isinstance(output, list) and output and not _is_error(output):
        as_of = _day_start(seen_on)
    if as_of:
        item["as_of"] = as_of
    if used is not None:
        item["used"] = used
    h = content_hash(output)
    if h:
        item["hash"] = h
    return item


def _retrieval_count(entries: list) -> Optional[int]:
    total, seen = 0, False
    for e in entries:
        returned = (e.get("meta") or {}).get("returned")
        if isinstance(returned, int) and not _is_error(e["output"]):
            total += returned          # the tool counted its own lookup before any cap of its own
            seen = True
        elif e["name"] in _RETRIEVAL_TOOLS and isinstance(e["output"], list) and not _is_error(e["output"]):
            total += len(e["output"])
            seen = True
    return total if seen else None


def _fit(manifest: dict) -> dict:
    """Drop items from the end until the JSON is within MAX_BYTES, and say so."""
    def size(m: dict) -> int:
        return len(json.dumps(m, separators=(",", ":"), default=str).encode("utf-8"))
    items = manifest.get("items")
    if isinstance(items, list) and len(items) > MAX_ITEMS:
        del items[MAX_ITEMS:]
        manifest["truncated"] = True
    while size(manifest) > MAX_BYTES and isinstance(items, list) and items:
        items.pop()
        manifest["truncated"] = True
    return manifest


def build(agent: str, entries: list, inputs: list, has_model: bool) -> Optional[dict]:
    """The manifest for one decision step, or None when nothing is known. `entries`: reads {name, output, used?, kind?}.
    `inputs`: upstream steps {agent, span_id}. Never raises."""
    try:
        items: list = []
        for up in inputs or []:
            item: dict = {"kind": "input", "source": f"step:{up.get('agent') or 'unknown'}", "used": True}
            if up.get("span_id"):
                item["id"] = up["span_id"]
            items.append(item)
        for e in entries or []:
            items.append(tool_item(e["name"], e["output"], used=e.get("used"), kind=e.get("kind"),
                                   meta=e.get("meta"), seen_on=e.get("seen_on")))
        out: dict = {"v": 1}
        if items:
            out["items"] = items
        retrieval = _retrieval_count(entries or [])
        if retrieval is not None:
            out["retrieval"] = {"returned": retrieval}
        if has_model:
            ins = instruction_for(agent)
            if ins:
                out["instruction"] = ins
        # Only a tool that dropped rows from what it handed the model says so; nothing is inferred.
        if any((e.get("meta") or {}).get("cut") for e in entries or [] if not _is_error(e["output"])):
            out["truncated"] = True
        if len(out) == 1:
            return None
        return _fit(out)
    except Exception:
        return None


class Recorder:
    """What one TraceLogger has seen since each agent's last decision step. Thread safe: the research fan-out logs from a pool."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._pending: dict = {}
        self._span_agent: dict = {}

    def record_tool(self, agent: str, name: str, output: Any, key: Optional[str] = None) -> None:
        if not _READ_TOOL.match(name or ""):
            return
        self.note(agent, name, output, meta=take_stamp(key))

    def note(self, agent: str, name: str, output: Any, used: Optional[bool] = None, kind: Optional[str] = None,
             meta: Optional[dict] = None) -> None:
        with self._lock:
            bucket = self._pending.setdefault(agent, [])
            if len(bucket) < PENDING_CAP:
                # `seen_on` is the runner's date at the moment of the read: the same `date.today()` the tool's own query used.
                bucket.append({"name": name, "output": output, "used": used, "kind": kind, "meta": meta,
                               "seen_on": datetime.now().date().isoformat()})

    def take(self, agent: str) -> list:
        with self._lock:
            return self._pending.pop(agent, [])

    def remember_span(self, span_id: str, agent: str) -> None:
        if span_id:
            with self._lock:
                self._span_agent[span_id] = base_agent(agent)

    def upstream(self, span_ids: Optional[list]) -> list:
        out = []
        with self._lock:
            for sid in span_ids or []:
                if isinstance(sid, str) and len(sid) == 16:
                    out.append({"agent": self._span_agent.get(sid), "span_id": sid})
        return out


def circuit_breaker_context(vix_data: Any, futures_data: Any) -> Optional[dict]:
    """The manifest for the market agent's circuit-breaker decision: made in code from these two reads, no model, so no instruction.
    Both reads are marked used because the code decided on exactly them. None unless the switch is on. Never raises."""
    try:
        if not enabled():
            return None
        return build("market", [{"name": "get_vix", "output": vix_data, "used": True},
                                {"name": "get_futures", "output": futures_data, "used": True}], [], has_model=False)
    except Exception:
        return None
