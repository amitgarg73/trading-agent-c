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

_TENANT_ID = os.environ.get("TENANT_ID", "")
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
        result = (
            get_client()
            .table("ag_eval_configs")
            .select("id, eval_name, agent, threshold, config")
            .eq("tenant_id", _TENANT_ID)
            .eq("eval_type", "semantic")
            .eq("enabled", True)
            .execute()
        )
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

    try:
        verdict = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        verdict = {"score": 0, "passed": False, "reasoning": "Judge parse error"}

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
            "id":         str(uuid4()),
            "tenant_id":  _TENANT_ID,
            "session_id": session_id,
            "eval_name":  r["eval_name"],
            "agent":      agent,
            "layer":      4,
            "score":      r["score"],
            "passed":     r["passed"],
            "threshold":  r["threshold"],
            "detail":     {"reasoning": r["reasoning"]},
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
    avg_shortfall = sum(r["threshold"] - r["score"] for r in failed) / len(failed)
    severity = "high" if avg_shortfall > 0.3 else "medium"

    get_client().table("ag_incidents").insert({
        "id":           str(uuid4()),
        "tenant_id":    _TENANT_ID,
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
