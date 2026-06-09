"""
Generic LLM-as-judge for AI agent output quality evaluation.

Fetches semantic eval criteria from ag_eval_configs (Argus UI-managed, tenant-scoped).
Scores each agent's output against its criteria using claude-haiku (fast, cheap).
Writes results to ag_evals. Creates ag_incidents rows when criteria fail.

No domain-specific logic — works for any pipeline using the Argus observability schema.
"""
from __future__ import annotations

import json
import os
from typing import Optional
from uuid import uuid4

import anthropic

_TENANT_ID   = os.environ.get("TENANT_ID",   "")
_WORKFLOW_ID = os.environ.get("WORKFLOW_ID", "")
_JUDGE_MODEL = "claude-haiku-4-5-20251001"

_JUDGE_SYSTEM = (
    "You are an independent quality evaluator for AI agent outputs. "
    "Score the agent output against the given criterion. Be concise and precise. "
    "Respond with valid JSON only — no other text."
)

_JUDGE_PROMPT = """\
Agent: {agent}
Agent output (truncated to 3000 chars):
{output}

Criterion: {criterion_name}
What to evaluate: {criterion_prompt}

Score this output from 0 to 10:
  10 = fully meets the criterion
  7-9 = mostly meets, minor gaps
  4-6 = partially meets
  1-3 = mostly fails
  0 = completely fails or missing

Respond with JSON only:
{{"score": <integer 0-10>, "passed": <bool>, "reasoning": "<one concise sentence>"}}"""


# ── Fetch criteria ─────────────────────────────────────────────────────────────

def _fetch_criteria(agent_names: list[str]) -> dict[str, list[dict]]:
    """
    Fetch enabled semantic eval configs for the given agent base names.
    Returns {agent_name: [config_row, ...]}
    """
    if not _TENANT_ID or not agent_names:
        return {}
    try:
        from core.db import get_client
        q = (
            get_client()
            .table("ag_eval_configs")
            .select("id, eval_name, agent, threshold, config")
            .eq("tenant_id", _TENANT_ID)
            .eq("eval_type", "semantic")
            .eq("enabled", True)
        )
        if _WORKFLOW_ID:
            q = q.eq("workflow_id", _WORKFLOW_ID)
        result = q.execute()
        rows = result.data or []
    except Exception:
        return {}

    by_agent: dict[str, list[dict]] = {}
    for row in rows:
        agent = row.get("agent")
        if agent and agent in agent_names:
            by_agent.setdefault(agent, []).append(row)
    return by_agent


# ── Single criterion judge call ────────────────────────────────────────────────

def _score_criterion(
    client: anthropic.Anthropic,
    agent: str,
    output: str,
    criterion: dict,
) -> dict:
    """
    Run one judge call for one criterion.
    Returns {eval_name, score (0-1), passed, threshold, reasoning}.
    """
    cfg       = criterion.get("config") or {}
    prompt    = cfg.get("prompt") or criterion.get("eval_name", "")
    threshold = float(criterion.get("threshold") or 0.7)

    user_msg = _JUDGE_PROMPT.format(
        agent=agent,
        output=output[:3000],
        criterion_name=criterion["eval_name"],
        criterion_prompt=prompt,
    )

    response = client.messages.create(
        model=_JUDGE_MODEL,
        max_tokens=256,
        system=_JUDGE_SYSTEM,
        messages=[{"role": "user", "content": user_msg}],
    )
    raw = next((b.text for b in response.content if hasattr(b, "text")), "{}")

    # Strip markdown code fences if present
    stripped = raw.strip()
    if stripped.startswith("```"):
        stripped = stripped.split("\n", 1)[-1]
        stripped = stripped.rsplit("```", 1)[0].strip()

    try:
        verdict = json.loads(stripped)
    except (json.JSONDecodeError, ValueError):
        verdict = {"score": 0, "passed": False, "reasoning": f"Judge parse error: {raw[:100]}"}

    raw_score      = max(0.0, min(10.0, float(verdict.get("score", 0))))
    score_norm     = round(raw_score / 10.0, 3)

    return {
        "eval_config_id": criterion["id"],
        "eval_name":      criterion["eval_name"],
        "score":          score_norm,
        "passed":         score_norm >= threshold,
        "threshold":      threshold,
        "reasoning":      str(verdict.get("reasoning", ""))[:500],
    }


# ── Persist results ────────────────────────────────────────────────────────────

def _write_eval_results(session_id: str, agent: str, results: list[dict]) -> None:
    from core.db import get_client
    rows = [
        {
            "id":          str(uuid4()),
            "tenant_id":   _TENANT_ID,
            "workflow_id": _WORKFLOW_ID,
            "session_id":  session_id,
            "eval_name":   r["eval_name"],
            "agent":       agent,
            "layer":       4,
            "score":       r["score"],
            "passed":      r["passed"],
            "threshold":   r["threshold"],
            "detail":      {"reasoning": r["reasoning"]},
        }
        for r in results
    ]
    if rows:
        get_client().table("ag_evals").insert(rows).execute()


def _write_incident_if_failed(
    session_id: str,
    agent: str,
    all_results: list[dict],
) -> None:
    failed = [r for r in all_results if not r["passed"]]
    if not failed:
        return

    from core.db import get_client
    db = get_client()

    # Dedup: skip if an incident already exists for this session + agent + pattern.
    # Two eval paths (agent outputs + trace reasoning) both call this function,
    # which would otherwise produce duplicate rows for the same failure.
    existing = (
        db.table("ag_incidents")
        .select("id")
        .eq("tenant_id", _TENANT_ID)
        .eq("session_id", session_id)
        .eq("pattern_name", "semantic_quality_failure")
        .ilike("root_cause", f"{agent} output%")
        .limit(1)
        .execute()
    )
    if existing.data:
        return

    avg_shortfall = sum(r["threshold"] - r["score"] for r in failed) / len(failed)
    severity = "high" if avg_shortfall > 0.3 else "medium"

    db.table("ag_incidents").insert({
        "id":           str(uuid4()),
        "tenant_id":    _TENANT_ID,
        "workflow_id":  _WORKFLOW_ID,
        "session_id":   session_id,
        "pattern_name": "semantic_quality_failure",
        "severity":     severity,
        "root_cause":   (
            f"{agent} output failed {len(failed)} of {len(all_results)} "
            f"quality criteria (semantic eval)"
        ),
        "failed_evals": [
            {
                "agent":      agent,
                "eval_name":  r["eval_name"],
                "score":      r["score"],
                "threshold":  r["threshold"],
                "reasoning":  r["reasoning"],
            }
            for r in failed
        ],
        "fix_suggestion": (
            f"Review {agent} output quality. "
            f"Failing: {', '.join(r['eval_name'] for r in failed)}. "
            "Adjust criteria or agent prompt in Argus Eval Manager."
        ),
        "status": "open",
    }).execute()


# ── Public API ─────────────────────────────────────────────────────────────────

def evaluate_session_outputs(
    session_id: str,
    agent_outputs: dict[str, str],
    tracer: Optional[object] = None,  # reserved for future span logging
) -> dict[str, list[dict]]:
    """
    Evaluate agent outputs against configured semantic eval criteria.

    Call this once per session, after all agents complete and before close_session().

    agent_outputs: {agent_base_name: output_text_or_json_string}
      Agent names must match ag_eval_configs.agent (e.g. "research", not "research_ABBV").
      Output values are truncated to 3000 chars before scoring.

    Returns: {agent_name: [{eval_name, score, passed, threshold, reasoning}]}
    Empty dict if no criteria are configured or TENANT_ID is unset.
    """
    if not _TENANT_ID or not agent_outputs:
        return {}

    criteria_by_agent = _fetch_criteria(list(agent_outputs.keys()))
    if not criteria_by_agent:
        return {}

    client = anthropic.Anthropic()
    all_results: dict[str, list[dict]] = {}

    for agent, criteria in criteria_by_agent.items():
        output = agent_outputs.get(agent, "")
        if not output or not criteria:
            continue

        agent_results: list[dict] = []
        for criterion in criteria:
            try:
                result = _score_criterion(client, agent, output, criterion)
                agent_results.append(result)
            except Exception as exc:
                agent_results.append({
                    "eval_name": criterion.get("eval_name", "unknown"),
                    "score":     0.0,
                    "passed":    False,
                    "threshold": float(criterion.get("threshold") or 0.7),
                    "reasoning": f"Judge error: {exc}",
                })

        if agent_results:
            try:
                _write_eval_results(session_id, agent, agent_results)
                _write_incident_if_failed(session_id, agent, agent_results)
            except Exception as exc:
                print(f"[judge] DB write failed for {agent}: {exc}")
            all_results[agent] = agent_results

    return all_results


def evaluate_session_from_traces(session_id: str) -> None:
    """
    Read agent reasoning from ag_traces (agent_message rows) and run LLM judge.
    Uses payload->agent_reasoning as judge input — not the outcome label.
    Call after close_session(). Non-blocking — logs failures, does not raise.
    """
    if not _TENANT_ID:
        return
    try:
        from core.db import get_client
        q = (
            get_client()
            .table("ag_traces")
            .select("agent, step_type, payload")
            .eq("tenant_id", _TENANT_ID)
            .eq("session_id", session_id)
            .eq("step_type", "agent_message")
            .limit(200)
        )
        if _WORKFLOW_ID:
            q = q.eq("workflow_id", _WORKFLOW_ID)
        rows = q.execute().data or []
        if not rows:
            return

        TICKER_SUFFIX = __import__("re").compile(r"^[A-Z]{1,5}$")

        def _base(name: str) -> str:
            parts = name.split("_")
            if len(parts) > 1 and TICKER_SUFFIX.match(parts[-1]):
                return "_".join(parts[:-1])
            return name

        agent_outputs: dict[str, list[str]] = {}
        for row in rows:
            agent     = _base((row.get("agent") or "").strip())
            payload   = row.get("payload") or {}
            reasoning = str(payload.get("agent_reasoning") or "").strip()
            if agent and reasoning:
                agent_outputs.setdefault(agent, []).append(reasoning)

        combined = {
            agent: " | ".join(outputs[:5])
            for agent, outputs in agent_outputs.items()
            if outputs
        }
        if combined:
            evaluate_session_outputs(session_id, combined)
    except Exception as exc:
        print(f"[judge] evaluate_session_from_traces failed: {exc}")
