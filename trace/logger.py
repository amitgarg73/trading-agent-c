from __future__ import annotations

import os
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

_TENANT_ID   = _load_env_var("TENANT_ID")
_WORKFLOW_ID  = _load_env_var("WORKFLOW_ID")

# Token cost per million tokens (Anthropic pricing, mid-2026)
# cache_read = prompt cache hit; cache_write = cache creation (first write)
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


class TraceLogger:
    """
    Writes structured trace rows to ag_traces and a summary row to ag_sessions.

    Usage:
        tracer = TraceLogger(session_id)
        span_id = tracer.start_agent_span("market")
        tracer.log_tool_call("market", "get_vix", {}, result, latency_ms=220)
        tracer.log_agent_message("market", reasoning, "go", tokens_input=400, tokens_output=80)
        tracer.close_session("converged", trades_proposed=3, trades_approved=2, trades_executed=2)
    """

    def __init__(self, session_id: str, workflow_id: Optional[str] = None, session_type: Optional[str] = None):
        self.session_id = session_id
        self._workflow_id = workflow_id or _WORKFLOW_ID or None
        self._session_type = session_type
        self._sequence = 0
        self._agent_spans: dict[str, str] = {}   # agent -> current span_id
        self._session_span_id = str(uuid4())
        self._tokens: dict[str, dict[str, int]] = {}   # agent -> {input, output}
        self._pending_trades: list = []
        self._started_at = datetime.utcnow()
        self._insert_session_stub()

    def set_pending_trades(self, trades: list) -> None:
        """Store deferred pre-open trades so close_session persists them in ag_sessions metadata."""
        self._pending_trades = trades

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
        or a plain dict. Captures cache tokens for accurate cost calculation.
        Written to ag_sessions at close_session().
        """
        if hasattr(usage, "input_tokens"):
            inp   = usage.input_tokens
            out   = usage.output_tokens
            cr    = getattr(usage, "cache_read_input_tokens",    0) or 0
            cw    = getattr(usage, "cache_creation_input_tokens", 0) or 0
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
        """
        Normalize an OTel span emitted by a TypeScript agent and insert into ag_traces.
        Called by the session driver for each OTEL_SPAN: line from subprocess stdout.
        """
        from trace.normalizer import normalize_otel_span
        self._sequence += 1
        row = normalize_otel_span(span, self._sequence, self.session_id, _TENANT_ID)
        if row:
            from core.db import get_client
            get_client().table("ag_traces").insert(row).execute()

    def flush_cost_breakdown(self) -> None:
        """
        Upsert the current accumulated cost breakdown to ag_sessions without closing
        the session. Call after each major agent completes so partial cost is captured
        even if the process is killed before close_session().
        """
        if not self._tokens:
            return
        from core.db import get_client
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
        get_client().table("ag_sessions").update({
            "metadata":       {"cost_breakdown": agent_costs},
            "total_cost_usd": round(total_cost, 6),
        }).eq("id", self.session_id).execute()

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
        """Finalize the ag_sessions row for this session.

        Uses update() so callers that attach to an existing session (EOD, intraday)
        never overwrite started_at or token counts set by premarket.
        Cost/token fields are only written when this TraceLogger actually logged tokens.
        """
        from core.db import get_client

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

        row: dict[str, Any] = {
            "terminal_reason": terminal_reason,
            "ended_at":        completed_at.isoformat(),
            "status":          "completed",
            "metadata":        metadata,
        }
        if result_summary:
            row["result_summary"] = result_summary

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
            row.update({
                "total_tokens_input":  total_input,
                "total_tokens_output": total_output,
                "total_cost_usd":      round(total_cost, 6),
            })
            metadata.update({
                "agents_invoked":   agents_invoked or list(self._tokens.keys()),
                "loop_iterations":  loop_iterations,
                "total_tool_calls": self._count_tool_calls(),
                "cost_breakdown":   agent_costs,
            })

        get_client().table("ag_sessions").update(row).eq("id", self.session_id).execute()
        self._trigger_embeddings()

    def _trigger_embeddings(self) -> None:
        """Fire-and-forget POST to Argus embedding compute route at session close.
        Runs in a daemon thread — never blocks session close or trade execution."""
        import threading, urllib.request, json as _json
        argus_url = os.getenv("ARGUS_URL", "")
        if not argus_url:
            return

        def _post() -> None:
            try:
                payload = _json.dumps({
                    "session_id":  self.session_id,
                    "tenant_id":   _TENANT_ID,
                    "workflow_id": self._workflow_id,
                }).encode()
                req = urllib.request.Request(
                    f"{argus_url.rstrip('/')}/api/compute/embeddings",
                    data=payload,
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                urllib.request.urlopen(req, timeout=30)
            except Exception as e:
                print(f"  [tracer] embedding trigger failed (non-fatal): {e}")

        t = threading.Thread(target=_post, daemon=True)
        t.start()

    # ── Private ────────────────────────────────────────────────────────────────

    def _insert_session_stub(self) -> None:
        """Upsert a minimal ag_sessions row so ag_traces FK is satisfied from the start."""
        from core.db import get_client
        stub: dict[str, Any] = {
            "id":              self.session_id,
            "tenant_id":       _TENANT_ID,
            "workflow_id":     self._workflow_id,
            "started_at":      self._started_at.isoformat(),
            "status":          "in_progress",
            "terminal_reason": "in_progress",
        }
        if self._session_type:
            stub["session_type"] = self._session_type
        get_client().table("ag_sessions").upsert(stub, on_conflict="id", ignore_duplicates=True).execute()

    def _write(self, fields: dict) -> str:
        from core.db import get_client
        span_id = str(uuid4())
        self._sequence += 1
        agent = fields.get("agent", "orchestrator")

        # Auto-derive entity_id for research sub-agents (pattern: research_TICKER)
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
        }
        if fields.get("tool_input") is not None:
            payload["tool_input"] = fields["tool_input"]
        if fields.get("tool_output") is not None:
            payload["tool_output"] = fields["tool_output"]
        if fields.get("agent_reasoning") is not None:
            payload["agent_reasoning"] = fields["agent_reasoning"]

        row: dict[str, Any] = {
            "tenant_id":    _TENANT_ID,
            "workflow_id":  self._workflow_id,
            "session_id":   self.session_id,
            "agent":        agent,
            "step_type":    fields.get("step_type"),
            "tool_name":    fields.get("tool_name"),
            "outcome":      fields.get("outcome"),
            "error":        fields.get("error"),
            "latency_ms":   fields.get("latency_ms", 0),
            "tokens_input": tokens_input,
            "tokens_output": tokens_output,
            "cost_usd":     cost_usd,
            "payload":      payload,
            "created_at":   datetime.utcnow().isoformat(),
        }
        get_client().table("ag_traces").insert(row).execute()
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
    # research mini-agents log as "research_TICKER" — strip suffix before matching
    base = agent.split("_")[0] if "_" in agent else agent
    sonnet_agents = {"orchestrator", "learning"}
    return "claude-sonnet-4-6" if base in sonnet_agents else "claude-haiku-4-5-20251001"
