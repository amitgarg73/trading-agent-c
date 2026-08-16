from __future__ import annotations

import json
import os
import threading
import urllib.request
from datetime import date, datetime
from typing import Any, Optional

import pytz  # noqa: F401 (used downstream by callers)

# OTel imports — required; raises ImportError with a clear message if missing
try:
    from opentelemetry import trace as otel_trace
    from opentelemetry.sdk.trace import TracerProvider, ReadableSpan
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.trace import NonRecordingSpan, set_span_in_context, Status, StatusCode
    import opentelemetry.context as otel_ctx
except ImportError as _e:  # pragma: no cover
    raise ImportError(
        "opentelemetry-api and opentelemetry-sdk are required. "
        "Run: pip install opentelemetry-api opentelemetry-sdk"
    ) from _e

def _load_env_var(key: str) -> str:
    val = os.environ.get(key, "")
    if not val:
        try:
            from dotenv import load_dotenv as _ld
            _ld()
            val = os.environ.get(key, "")
        except ImportError:
            pass
    return val

_TENANT_ID    = _load_env_var("TENANT_ID")
_WORKFLOW_ID  = _load_env_var("WORKFLOW_ID")
_ARGUS_URL    = _load_env_var("ARGUS_URL").rstrip("/")
_ARGUS_API_KEY = _load_env_var("ARGUS_API_KEY")


def _emit_enabled() -> bool:
    """Whether telemetry may be sent to Provy.

    Requires ARGUS_URL + ARGUS_API_KEY AND an explicit opt-in, so a local or
    agent-driven run never writes to production Provy just because a .env carries
    prod credentials. Opt-in is PROVY_EMIT (truthy). GitHub Actions is treated as
    an automatic opt-in so the scheduled prod workflows keep reporting with no
    extra config. Default off: dev and ad-hoc runs stay silent.
    """
    if not (_ARGUS_URL and _ARGUS_API_KEY):
        return False
    if os.environ.get("PROVY_EMIT", "").strip().lower() in ("1", "true", "yes", "on"):
        return True
    if os.environ.get("GITHUB_ACTIONS", "").strip().lower() == "true":
        return True
    return False

# Token cost per million tokens (Anthropic pricing, mid-2026)
_COST_PER_MTOK: dict[str, dict[str, float]] = {
    "claude-haiku-4-5-20251001": {"input": 0.80, "output": 4.00, "cache_read": 0.08,  "cache_write": 1.00},
    "claude-sonnet-4-6":         {"input": 3.00, "output": 15.00, "cache_read": 0.30, "cache_write": 3.75},
}


def _estimate_cost(
    model: str,
    input_tokens: int,
    output_tokens: int,
    cache_read_tokens: int = 0,
    cache_write_tokens: int = 0,
) -> float:
    rates = _COST_PER_MTOK.get(model, {"input": 3.00, "output": 15.00, "cache_read": 0.30, "cache_write": 3.75})
    return (
        input_tokens       * rates["input"]       +
        output_tokens      * rates["output"]       +
        cache_read_tokens  * rates["cache_read"]   +
        cache_write_tokens * rates["cache_write"]
    ) / 1_000_000


def _ingest_post(path: str, payload: dict) -> bool:
    """POST to Argus ingest API. Non-fatal on any error.

    Returns True only if the request was actually accepted. Callers that merely
    emit telemetry can keep ignoring this; callers that report a business outcome
    must not, because a dropped outcome is a fact the fleet loses silently.
    """
    if not _emit_enabled():
        return False
    return _ingest_post_raw(path, json.dumps(payload).encode())


def _ingest_post_raw(path: str, data: bytes) -> bool:
    """POST with pre-encoded bytes. Non-fatal on any error. True if accepted.

    This used to swallow every exception and return nothing, so a caller counting
    its own loop iterations reported deliveries it never made. The retired
    argusobs host still answers 200, so a stale ARGUS_URL fails this way too:
    quietly, and looking exactly like success.
    """
    if not _emit_enabled():
        return False
    try:
        req = urllib.request.Request(
            f"{_ARGUS_URL}{path}",
            data=data,
            headers={"Content-Type": "application/json", "x-argus-key": _ARGUS_API_KEY},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10) as r:
            return 200 <= r.status < 300
    except Exception as e:
        print(f"[argus] POST {path} failed: {e}")
        return False


def _ingest_patch(path: str, payload: dict) -> None:
    """Fire-and-forget PATCH to Argus ingest API. Non-fatal on any error."""
    if not _emit_enabled():
        return
    try:
        data = json.dumps(payload).encode()
        req = urllib.request.Request(
            f"{_ARGUS_URL}{path}",
            data=data,
            headers={"Content-Type": "application/json", "x-argus-key": _ARGUS_API_KEY},
            method="PATCH",
        )
        urllib.request.urlopen(req, timeout=10)
    except Exception:
        pass


def _ingest_get(path: str, params: dict) -> dict:
    """GET from Argus ingest API. Returns parsed JSON or {} on any error."""
    if not _emit_enabled():
        return {}
    try:
        from urllib.parse import urlencode
        url = f"{_ARGUS_URL}{path}?{urlencode(params)}" if params else f"{_ARGUS_URL}{path}"
        req = urllib.request.Request(
            url,
            headers={"x-argus-key": _ARGUS_API_KEY},
            method="GET",
        )
        resp = urllib.request.urlopen(req, timeout=10)
        return json.loads(resp.read().decode())
    except Exception:
        return {}


class TraceLogger:
    """
    Writes structured trace rows to the Argus ingest API (ag_traces, ag_sessions).

    Usage:
        tracer = TraceLogger(session_id)
        span_id = tracer.start_agent_span("market")
        tracer.log_tool_call("market", "get_vix", {}, result, latency_ms=220)
        tracer.log_agent_message("market", reasoning, "go", tokens_input=400, tokens_output=80)
        tracer.close_session("converged", trades_proposed=3, trades_approved=2, trades_executed=2)
    """

    def __init__(
        self,
        session_id: str,
        workflow_id: Optional[str] = None,
        session_type: Optional[str] = None,
        parent_session_id: Optional[str] = None,
        is_simulated: bool = False,
    ):
        self.session_id = session_id
        self._workflow_id = workflow_id or _WORKFLOW_ID or None
        self._session_type = session_type
        self._parent_session_id = parent_session_id
        self._sequence = 0
        self._tokens: dict[str, dict[str, int]] = {}
        self._pending_trades: list = []
        self._started_at = datetime.utcnow()

        # The agent's own record of this run, in its own database, written before any telemetry
        # leaves the process. Synchronous and allowed to raise: if the agent cannot remember that
        # it started a run, it must not go on to place trades it will later fail to recognise.
        # The ingest POST below is the opposite -- fire-and-forget, because a dropped trace costs
        # a dashboard row, not a position. Control flow reads this record and never reads Provy.
        from core import run_state
        run_state.open_run(
            session_id,
            session_type or "premarket",
            parent_run_id=parent_session_id,
            workflow_id=self._workflow_id,
            is_simulated=is_simulated,
        )

        # OTel setup — each TraceLogger gets its own provider so session spans
        # don't bleed across concurrent sessions.
        from trace.otel_exporter import ArgusExporter
        self._otel_provider = TracerProvider()
        if _emit_enabled():
            self._otel_provider.add_span_processor(
                SimpleSpanProcessor(ArgusExporter(api_key=_ARGUS_API_KEY, endpoint=_ARGUS_URL))
            )
        self._tracer = self._otel_provider.get_tracer("trading-agent-c")

        # Session root span — all agent spans are children of this
        self._session_span = self._tracer.start_span(
            f"session:{session_type or 'premarket'}",
            attributes={"argus.session": "true", "argus.session_id": session_id},
        )
        self._session_ctx = set_span_in_context(self._session_span)

        # Map agent name → its OTel span (kept open until close_session)
        self._agent_otel_spans: dict[str, Any] = {}

        # Open session via ingest API — fire-and-forget; traces can flow immediately
        self._open_thread = threading.Thread(target=self._open_session, daemon=True)
        self._open_thread.start()

    def set_pending_trades(self, trades: list) -> None:
        self._pending_trades = trades

    # ── Public API ─────────────────────────────────────────────────────────────

    def start_agent_span(self, agent: str) -> str:
        """Create an OTel child span for this agent under the session root."""
        span = self._tracer.start_span(
            f"agent:{agent}",
            context=self._session_ctx,
            attributes={"argus.session_id": self.session_id, "argus.agent": agent},
        )
        self._agent_otel_spans[agent] = span
        sc = span.get_span_context()
        return format(sc.span_id, "016x") if sc else agent

    def get_agent_span(self, agent: str) -> Optional[str]:
        span = self._agent_otel_spans.get(agent)
        if not span:
            return None
        sc = span.get_span_context()
        return format(sc.span_id, "016x") if sc else None

    def log_tool_call(
        self,
        agent: str,
        tool_name: str,
        tool_input: dict,
        tool_output: Any,
        entity_id: Optional[str] = None,
        latency_ms: int = 0,
        model: Optional[str] = None,
    ) -> str:
        return self._write({
            "step_type":   "tool_call",
            "agent":       agent,
            "tool_name":   tool_name,
            "tool_input":  tool_input,
            "tool_output": tool_output if isinstance(tool_output, dict) else {"value": tool_output},
            "entity_id":   entity_id,
            "latency_ms":  latency_ms,
            "model":       model,
        })

    def log_agent_message(
        self,
        agent: str,
        reasoning: str,
        outcome: str,
        entity_id: Optional[str] = None,
        tokens_input: int = 0,
        tokens_output: int = 0,
        model: Optional[str] = None,
        latency_ms: int = 0,
        payload: Optional[dict] = None,
    ) -> str:
        """Log an agent's message.

        `payload` carries STRUCTURED scalars alongside the prose, emitted as argus.payload.<key>.

        ⛔ A NUMBER INSIDE `reasoning` IS INVISIBLE TO PROVY. The reasoning is one text blob, so
        anything stated only in there cannot be read as a signal, bound to a contract condition, or
        compared with what settled. The orchestrator computed `total_estimated_profit` for months and
        Provy never saw it: the trace registry held `entry_price` but not the estimate, so Provy fell
        back to forecasting from judge scores instead of using the agent's own number (argus#601).

        If a value is meant to be graded or compared, it goes here, not only in the prose.
        """
        return self._write({
            "step_type":       "agent_message",
            "agent":           agent,
            "agent_reasoning": reasoning,
            "outcome":         outcome,
            "entity_id":       entity_id,
            "tokens_input":    tokens_input,
            "tokens_output":   tokens_output,
            "latency_ms":      latency_ms,
            "model":           model,
            "payload":         payload,
        })

    def log_decision(
        self,
        agent: str,
        outcome: str,
        detail: Optional[dict] = None,
        latency_ms: int = 0,
        model: Optional[str] = None,
    ) -> str:
        return self._write({
            "step_type":   "decision",
            "agent":       agent,
            "outcome":     outcome,
            "tool_output": detail,
            "latency_ms":  latency_ms,
            "model":       model,
        })

    def log_skip(
        self,
        agent: str,
        reason: str,
        skip_type: str = "design",
    ) -> str:
        return self._write({
            "step_type": "skip",
            "agent":     agent,
            "outcome":   "skipped",
            "payload":   {"reason": reason, "skip_type": skip_type},
        })

    def log_error(
        self,
        agent: str,
        error_message: str,
        entity_id: Optional[str] = None,
    ) -> str:
        return self._write({
            "step_type": "error",
            "agent":     agent,
            "error":     error_message,
            "entity_id": entity_id,
            "outcome":   "error",
        })

    def log_tokens(self, agent: str, usage: Any) -> None:
        if hasattr(usage, "input_tokens"):
            inp = usage.input_tokens
            out = usage.output_tokens
            cr  = getattr(usage, "cache_read_input_tokens",    0) or 0
            cw  = getattr(usage, "cache_creation_input_tokens", 0) or 0
        else:
            inp = usage.get("input_tokens", 0)
            out = usage.get("output_tokens", 0)
            cr  = usage.get("cache_read_input_tokens",    0) or 0
            cw  = usage.get("cache_creation_input_tokens", 0) or 0

        if agent not in self._tokens:
            self._tokens[agent] = {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0}
        self._tokens[agent]["input"]       += inp
        self._tokens[agent]["output"]      += out
        self._tokens[agent]["cache_read"]  += cr
        self._tokens[agent]["cache_write"] += cw

    def ingest_otel_span(self, span: dict) -> None:
        """Forward an OTel span from the TypeScript News Analyst to the Argus OTLP gateway.
        The span must already carry argus.session_id as an attribute."""
        # Attach session_id if the span doesn't already have it
        attrs = span.get("attributes") or []
        has_session = any(a.get("key") == "argus.session_id" for a in attrs)
        if not has_session:
            attrs = list(attrs) + [{"key": "argus.session_id", "value": {"stringValue": self.session_id}}]
            span = {**span, "attributes": attrs}

        payload = json.dumps({"resourceSpans": [{"scopeSpans": [{"spans": [span]}]}]}).encode()
        _ingest_post_raw("/api/otlp/v1/traces", payload)

    def flush_cost_breakdown(self) -> None:
        """Persist accumulated cost mid-session via ingest PATCH. Ensures cost is captured if process is killed."""
        if not self._tokens:
            return
        agent_costs = {}
        for agent, v in self._tokens.items():
            model = _agent_model(agent)
            cost  = _estimate_cost(
                model,
                v["input"],
                v["output"],
                v.get("cache_read",  0),
                v.get("cache_write", 0),
            )
            agent_costs[agent] = {
                "model":       model,
                "input":       v["input"],
                "output":      v["output"],
                "cache_read":  v.get("cache_read",  0),
                "cache_write": v.get("cache_write", 0),
                "cost_usd":    round(cost, 6),
            }
        total_cost = sum(a["cost_usd"] for a in agent_costs.values())
        _ingest_patch("/api/ingest/session", {
            "session_id":    self.session_id,
            "total_cost_usd": round(total_cost, 6),
            "cost_breakdown": agent_costs,
        })

    def close_session(
        self,
        terminal_reason: str,
        agents_invoked: Optional[list[str]] = None,
        loop_iterations: int = 1,
        trades_proposed: int = 0,
        trades_approved: int = 0,
        trades_executed: int = 0,
        risk_rejections: int = 0,
        retry_triggered: bool = False,
        result_summary: Optional[str] = None,
    ) -> None:
        """Close session via ingest API — triggers embeddings + diagnosis inline."""
        completed_at = datetime.utcnow()
        latency_ms   = int((completed_at - self._started_at).total_seconds() * 1000)

        metadata: dict[str, Any] = {
            "date":             date.today().isoformat(),
            "total_steps":      self._sequence,
            "trades_proposed":  trades_proposed,
            "trades_approved":  trades_approved,
            "trades_executed":  trades_executed,
            "risk_rejections":  risk_rejections,
            "retry_triggered":  retry_triggered,
            "total_latency_ms": latency_ms,
        }
        if self._pending_trades:
            metadata["pending_trades"] = self._pending_trades

        # Close the agent's own record first. Premarket's concurrency guard reads this to tell a
        # finished run from one still in flight, and the watchdog reads pending_trades from it to
        # place entries deferred past the opening bell. Both must survive Provy being unreachable.
        from core import run_state
        run_state.close_run(
            self.session_id,
            terminal_reason,
            result_summary=result_summary,
            trades_proposed=trades_proposed,
            trades_approved=trades_approved,
            trades_executed=trades_executed,
            risk_rejections=risk_rejections,
            agents_invoked=agents_invoked,
            loop_iterations=loop_iterations,
            retry_triggered=retry_triggered,
            total_steps=self._sequence,
        )
        if self._pending_trades:
            run_state.set_pending_trades(self.session_id, self._pending_trades)

        body: dict[str, Any] = {
            "session_id":     self.session_id,
            "terminal_reason": terminal_reason,
            "metadata":       metadata,
        }
        if result_summary:
            body["result_summary"] = result_summary

        if self._tokens:
            agent_costs: dict[str, Any] = {}
            for agent, v in self._tokens.items():
                model = _agent_model(agent)
                cost  = _estimate_cost(
                    model,
                    v["input"],
                    v["output"],
                    v.get("cache_read",  0),
                    v.get("cache_write", 0),
                )
                agent_costs[agent] = {
                    "model":        model,
                    "input":        v["input"],
                    "output":       v["output"],
                    "cache_read":   v.get("cache_read",  0),
                    "cache_write":  v.get("cache_write", 0),
                    "cost_usd":     round(cost, 6),
                }
            total_cost   = sum(a["cost_usd"] for a in agent_costs.values())
            total_input  = sum(v["input"]    for v in self._tokens.values())
            total_output = sum(v["output"]   for v in self._tokens.values())
            body.update({
                "total_tokens_in":  total_input,
                "total_tokens_out": total_output,
                "total_cost_usd":   round(total_cost, 6),
            })
            metadata.update({
                "agents_invoked":   agents_invoked or list(self._tokens.keys()),
                "loop_iterations":  loop_iterations,
                "total_tool_calls": self._count_tool_calls(),
                "cost_breakdown":   agent_costs,
            })

        # End all open agent spans, then the session root span
        for span in self._agent_otel_spans.values():
            try: span.end()
            except Exception: pass
        try: self._session_span.end()
        except Exception: pass

        # Force-flush the OTel provider so all spans ship before the process exits
        self._otel_provider.force_flush(timeout_millis=8000)

        # Synchronous — waits for ingest close to fire diagnosis + embeddings
        _ingest_post("/api/ingest/session/close", body)

    # ── Private ────────────────────────────────────────────────────────────────

    def _open_session(self) -> None:
        """POST to ingest session/open. Runs as daemon thread — client already holds session_id."""
        _ingest_post("/api/ingest/session/open", {
            "session_id":       self.session_id,
            "session_type":     self._session_type or "premarket",
            "parent_session_id": self._parent_session_id,
            "started_at":       self._started_at.isoformat(),
            "metadata":         {"date": date.today().isoformat()},
        })

    def _write(self, fields: dict) -> str:
        """Create an OTel span for this trace step and export it via ArgusExporter."""
        self._sequence += 1
        agent     = fields.get("agent", "orchestrator")
        step_type = fields.get("step_type", "tool_call")

        entity_id = fields.get("entity_id")
        if entity_id is None and "_" in agent and agent.split("_", 1)[0] == "research":
            entity_id = agent.split("_", 1)[1].upper()

        tokens_input  = fields.get("tokens_input",  0)
        tokens_output = fields.get("tokens_output", 0)
        model         = fields.get("model")
        cost_usd: Optional[float] = None
        if tokens_input or tokens_output:
            cost_usd = round(_estimate_cost(
                model or _agent_model(agent),
                tokens_input,
                tokens_output,
            ), 8)

        # Parent context: use agent span if it exists, else fall back to session root
        parent_span = self._agent_otel_spans.get(agent)
        parent_ctx  = set_span_in_context(parent_span) if parent_span else self._session_ctx

        # Build OTel span attributes — these are mapped by the OTLP normalizer
        attrs: dict[str, Any] = {
            "argus.session_id": self.session_id,
            "argus.agent":      agent,
            "argus.step_type":  step_type,
            "argus.sequence":   self._sequence,
        }
        if fields.get("tool_name")  is not None: attrs["argus.tool_name"]      = fields["tool_name"]
        if fields.get("outcome")    is not None: attrs["argus.outcome"]         = str(fields["outcome"])
        if fields.get("error")      is not None: attrs["argus.error"]           = str(fields["error"])
        if fields.get("latency_ms") is not None: attrs["argus.latency_ms"]      = int(fields["latency_ms"])
        if tokens_input:                          attrs["llm.token_count.input"]  = tokens_input
        if tokens_output:                         attrs["llm.token_count.output"] = tokens_output
        if model:                                 attrs["argus.model"]            = model
        if cost_usd is not None:                  attrs["argus.cost_usd"]         = cost_usd
        if entity_id is not None:                 attrs["argus.entity_id"]        = entity_id

        # Payload fields serialised as JSON strings (OTLP attributes are scalars)
        if fields.get("agent_reasoning") is not None:
            attrs["argus.agent_reasoning"] = str(fields["agent_reasoning"])[:4000]
        if fields.get("tool_input") is not None:
            attrs["argus.tool_input"]  = json.dumps(fields["tool_input"],  default=str)[:4000]
        if fields.get("tool_output") is not None:
            attrs["argus.tool_output"] = json.dumps(
                fields["tool_output"] if isinstance(fields["tool_output"], dict) else {"value": fields["tool_output"]},
                default=str,
            )[:4000]
        if fields.get("payload") is not None:
            for k, v in fields["payload"].items():
                attrs[f"argus.payload.{k}"] = json.dumps(v, default=str) if not isinstance(v, (str, int, float, bool)) else v

        span = self._tracer.start_span(
            f"{step_type}:{fields.get('tool_name', agent)}",
            context=parent_ctx,
            attributes=attrs,
        )
        # Error steps must carry OTel ERROR status. The Argus ingest gateway derives the trace
        # outcome (and the error message) from span STATUS, not from attributes — so without this
        # an error trace is stored as outcome='success' with a null error, invisible to every
        # detector, the diagnosis, and the blind-spot check.
        if step_type == "error" or fields.get("outcome") == "error":
            span.set_status(Status(StatusCode.ERROR,
                                   str(fields.get("error") or fields.get("outcome") or "error")))
        span.end()

        sc = span.get_span_context()
        return format(sc.span_id, "016x") if sc else ""

    def _count_tool_calls(self) -> int:
        return self._sequence

    def get_sequence(self) -> int:
        return self._sequence

def _agent_model(agent: str) -> str:
    base = agent.split("_")[0] if "_" in agent else agent
    sonnet_agents = {"orchestrator", "learner"}
    return "claude-sonnet-4-6" if base in sonnet_agents else "claude-haiku-4-5-20251001"
