from __future__ import annotations

from datetime import date, datetime
from typing import Any, Optional
from uuid import uuid4

import pytz

# Token cost per million tokens (approximate, as of mid-2026)
_COST_PER_MTOK: dict[str, dict[str, float]] = {
    "claude-haiku-4-5-20251001": {"input": 0.80,  "output": 4.00},
    "claude-sonnet-4-6":         {"input": 3.00,  "output": 15.00},
}


def _estimate_cost(model: str, input_tokens: int, output_tokens: int) -> float:
    rates = _COST_PER_MTOK.get(model, {"input": 3.00, "output": 15.00})
    return (input_tokens * rates["input"] + output_tokens * rates["output"]) / 1_000_000


class TraceLogger:
    """
    Writes structured trace rows to c_traces and a summary row to c_sessions.

    Usage:
        tracer = TraceLogger(session_id)
        span_id = tracer.start_agent_span("market")
        tracer.log_tool_call("market", "get_vix", {}, result, latency_ms=220)
        tracer.log_agent_message("market", reasoning, "go", tokens_input=400, tokens_output=80)
        tracer.close_session("converged", trades_proposed=3, trades_approved=2, trades_executed=2)
    """

    def __init__(self, session_id: str):
        self.session_id = session_id
        self._sequence = 0
        self._agent_spans: dict[str, str] = {}   # agent -> current span_id
        self._session_span_id = str(uuid4())
        self._tokens: dict[str, dict[str, int]] = {}   # agent -> {input, output}
        self._started_at = datetime.utcnow()
        self._insert_session_stub()

    # ── Public API ─────────────────────────────────────────────────────────────

    def start_agent_span(self, agent: str) -> str:
        """
        Register a new span for this agent invocation.
        All subsequent log calls for this agent use this as parent_span_id.
        Returns the new span_id.
        """
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
        """Write a tool_call row. Returns the new span_id."""
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
        """Write an agent_message row. Returns the new span_id."""
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
        """Write a session-level decision row (no tool, no entity). Returns span_id."""
        return self._write({
            "step_type":   "decision",
            "agent":       agent,
            "outcome":     outcome,
            "tool_output": detail,
            "latency_ms":  latency_ms,
            "model":       model,
        })

    def log_error(
        self,
        agent: str,
        error_message: str,
        entity_id: Optional[str] = None,
    ) -> str:
        """Write an error row. Returns span_id."""
        return self._write({
            "step_type": "error",
            "agent":     agent,
            "error":     error_message,
            "entity_id": entity_id,
            "outcome":   "error",
        })

    def log_tokens(self, agent: str, usage: Any) -> None:
        """
        Accumulate token counts for an agent. `usage` is an Anthropic Usage object
        with .input_tokens and .output_tokens, or a plain dict with the same keys.
        Written to c_sessions at close_session().
        """
        if hasattr(usage, "input_tokens"):
            inp, out = usage.input_tokens, usage.output_tokens
        else:
            inp = usage.get("input_tokens", 0)
            out = usage.get("output_tokens", 0)

        if agent not in self._tokens:
            self._tokens[agent] = {"input": 0, "output": 0}
        self._tokens[agent]["input"]  += inp
        self._tokens[agent]["output"] += out

    def ingest_otel_span(self, span: dict) -> None:
        """
        Normalize an OTel span emitted by a TypeScript agent and insert into c_traces.
        Called by the session driver for each OTEL_SPAN: line from subprocess stdout.
        """
        from trace.normalizer import normalize_otel_span
        self._sequence += 1
        row = normalize_otel_span(span, self._sequence, self.session_id)
        if row:
            from core.db import get_client
            get_client().table("c_traces").insert(row).execute()

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
    ) -> None:
        """Write the c_sessions summary row. Call once at end of premarket session."""
        from core.db import get_client

        total_input  = sum(v["input"]  for v in self._tokens.values())
        total_output = sum(v["output"] for v in self._tokens.values())

        # Sum cost across agents (need to match agent→model for accuracy; use blended rate here)
        total_cost = sum(
            _estimate_cost(
                _agent_model(agent),
                v["input"],
                v["output"],
            )
            for agent, v in self._tokens.items()
        )

        completed_at = datetime.utcnow()
        latency_ms = int((completed_at - self._started_at).total_seconds() * 1000)

        row = {
            "id":                  self.session_id,
            "date":                date.today().isoformat(),
            "total_steps":         self._sequence,
            "total_tool_calls":    self._count_tool_calls(),
            "agents_invoked":      agents_invoked or list(self._tokens.keys()),
            "loop_iterations":     loop_iterations,
            "total_tokens_input":  total_input,
            "total_tokens_output": total_output,
            "total_cost_usd":      round(total_cost, 6),
            "total_latency_ms":    latency_ms,
            "trades_proposed":     trades_proposed,
            "trades_approved":     trades_approved,
            "trades_executed":     trades_executed,
            "risk_rejections":     risk_rejections,
            "retry_triggered":     retry_triggered,
            "terminal_reason":     terminal_reason,
            "started_at":          self._started_at.isoformat(),
            "completed_at":        completed_at.isoformat(),
        }
        get_client().table("c_sessions").upsert(row).execute()

    # ── Private ────────────────────────────────────────────────────────────────

    def _insert_session_stub(self) -> None:
        """Insert a minimal c_sessions row so c_traces FK is satisfied from the start."""
        from core.db import get_client
        get_client().table("c_sessions").insert({
            "id":              self.session_id,
            "date":            date.today().isoformat(),
            "terminal_reason": "in_progress",
            "started_at":      self._started_at.isoformat(),
        }).execute()

    def _write(self, fields: dict) -> str:
        from core.db import get_client
        span_id = str(uuid4())
        self._sequence += 1
        agent = fields.get("agent", "orchestrator")

        row: dict[str, Any] = {
            "session_id":      self.session_id,
            "span_id":         span_id,
            "parent_span_id":  self._agent_spans.get(agent),
            "entity_id":       fields.get("entity_id"),
            "date":            date.today().isoformat(),
            "sequence":        self._sequence,
            "agent":           agent,
            "step_type":       fields.get("step_type"),
            "tool_name":       fields.get("tool_name"),
            "tool_input":      fields.get("tool_input"),
            "tool_output":     fields.get("tool_output"),
            "agent_reasoning": fields.get("agent_reasoning"),
            "outcome":         fields.get("outcome"),
            "tokens_input":    fields.get("tokens_input", 0),
            "tokens_output":   fields.get("tokens_output", 0),
            "latency_ms":      fields.get("latency_ms", 0),
            "model":           fields.get("model"),
            "error":           fields.get("error"),
            "created_at":      datetime.utcnow().isoformat(),
        }
        get_client().table("c_traces").insert(row).execute()
        return span_id

    def _count_tool_calls(self) -> int:
        """Returns total tool_call rows written this session."""
        return self._sequence  # conservative — actual count tracked by sequence

    def get_sequence(self) -> int:
        return self._sequence

    def get_agent_span(self, agent: str) -> Optional[str]:
        return self._agent_spans.get(agent)


def _agent_model(agent: str) -> str:
    """Map agent name to its Claude model for cost estimation."""
    haiku_agents = {"market", "risk", "news_analyst"}
    return "claude-haiku-4-5-20251001" if agent in haiku_agents else "claude-sonnet-4-6"
