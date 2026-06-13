from __future__ import annotations

import json
import os
import threading
import urllib.request
from datetime import date, datetime
from typing import Any, Optional
from uuid import uuid4

import pytz

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


def _ingest_post(path: str, payload: dict) -> None:
    """Fire-and-forget POST to Argus ingest API. Non-fatal on any error."""
    if not _ARGUS_URL or not _ARGUS_API_KEY:
        return
    try:
        data = json.dumps(payload).encode()
        req = urllib.request.Request(
            f"{_ARGUS_URL}{path}",
            data=data,
            headers={"Content-Type": "application/json", "x-argus-key": _ARGUS_API_KEY},
            method="POST",
        )
        urllib.request.urlopen(req, timeout=10)
    except Exception:
        pass


def _ingest_patch(path: str, payload: dict) -> None:
    """Fire-and-forget PATCH to Argus ingest API. Non-fatal on any error."""
    if not _ARGUS_URL or not _ARGUS_API_KEY:
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
    if not _ARGUS_URL or not _ARGUS_API_KEY:
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
    ):
        self.session_id = session_id
        self._workflow_id = workflow_id or _WORKFLOW_ID or None
        self._session_type = session_type
        self._parent_session_id = parent_session_id
        self._sequence = 0
        self._agent_spans: dict[str, str] = {}
        self._session_span_id = str(uuid4())
        self._tokens: dict[str, dict[str, int]] = {}
        self._pending_trades: list = []
        self._started_at = datetime.utcnow()
        # Open session via ingest API — fire-and-forget; traces can flow immediately
        # because client already holds session_id.
        self._open_thread = threading.Thread(target=self._open_session, daemon=True)
        self._open_thread.start()

    def set_pending_trades(self, trades: list) -> None:
        self._pending_trades = trades

    # ── Public API ─────────────────────────────────────────────────────────────

    def start_agent_span(self, agent: str) -> str:
        span_id = str(uuid4())
        self._agent_spans[agent] = span_id
        return span_id

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
    ) -> str:
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
        """Normalize an OTel span from a TypeScript agent and POST to ingest API."""
        from trace.normalizer import normalize_otel_span
        self._sequence += 1
        row = normalize_otel_span(span, self._sequence, self.session_id, _TENANT_ID)
        if row:
            # Map normalised row fields to ingest trace payload
            _ingest_post("/api/ingest/trace", {
                "session_id": self.session_id,
                "agent":      row.get("agent", "news"),
                "step_type":  row.get("step_type", "tool_call"),
                "outcome":    row.get("outcome"),
                "latency_ms": row.get("latency_ms", 0),
                "payload":    row.get("payload"),
            })

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
        span_id = str(uuid4())
        self._sequence += 1
        agent = fields.get("agent", "orchestrator")

        entity_id = fields.get("entity_id")
        if entity_id is None and "_" in agent and agent.split("_", 1)[0] == "research":
            entity_id = agent.split("_", 1)[1].upper()

        tokens_input  = fields.get("tokens_input", 0)
        tokens_output = fields.get("tokens_output", 0)
        model         = fields.get("model")
        cost_usd: Optional[float] = None
        if tokens_input or tokens_output:
            cost_usd = round(_estimate_cost(
                model or _agent_model(agent),
                tokens_input,
                tokens_output,
            ), 8)

        payload: dict[str, Any] = {
            "span_id":        span_id,
            "parent_span_id": self._agent_spans.get(agent),
            "entity_id":      entity_id,
            "date":           date.today().isoformat(),
            "sequence":       self._sequence,
            "model":          model,
            "cost_usd":       cost_usd,
        }
        if fields.get("tool_input")      is not None: payload["tool_input"]      = fields["tool_input"]
        if fields.get("tool_output")     is not None: payload["tool_output"]     = fields["tool_output"]
        if fields.get("agent_reasoning") is not None: payload["agent_reasoning"] = fields["agent_reasoning"]
        if fields.get("payload")         is not None: payload.update(fields["payload"])

        _ingest_post("/api/ingest/trace", {
            "session_id": self.session_id,
            "agent":      agent,
            "step_type":  fields.get("step_type"),
            "tool_name":  fields.get("tool_name"),
            "outcome":    fields.get("outcome"),
            "error":      fields.get("error"),
            "latency_ms": fields.get("latency_ms", 0),
            "tokens_input":  tokens_input,
            "tokens_output": tokens_output,
            "payload":    payload,
        })
        return span_id

    def _count_tool_calls(self) -> int:
        return self._sequence

    def get_sequence(self) -> int:
        return self._sequence

    def get_agent_span(self, agent: str) -> Optional[str]:
        return self._agent_spans.get(agent)


def _agent_model(agent: str) -> str:
    base = agent.split("_")[0] if "_" in agent else agent
    sonnet_agents = {"orchestrator", "learning"}
    return "claude-sonnet-4-6" if base in sonnet_agents else "claude-haiku-4-5-20251001"
