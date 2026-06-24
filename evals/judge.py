"""
Generic LLM-as-judge for AI agent output quality evaluation.

Fetches semantic eval criteria from ag_eval_configs (Argus UI-managed, tenant-scoped).
Scores each agent's output against its criteria using claude-haiku (fast, cheap).
Writes results to ag_evals. Creates ag_incidents rows when criteria fail.

Called from _run_semantic_evals (agents/orchestrator.py) for both premarket and intraday
entry scans. Callers pre-process outputs before passing here so summary fields survive
the 3000-char judge window (see _prepare_scanner_for_judge, _prepare_risk_for_judge).

No domain-specific logic — works for any pipeline using the Argus observability schema.
"""
from __future__ import annotations

import json
from typing import Optional
from uuid import uuid4

import anthropic

_JUDGE_MODEL = "claude-haiku-4-5-20251001"

_JUDGE_SYSTEM = (
    "You are an independent quality evaluator for AI agent outputs. "
    "Score the agent output against the given criterion. Be concise and precise. "
    "Respond with valid JSON only — no other text."
)

_JUDGE_PROMPT = """\
Agent: {agent}
Agent output:
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

def _already_scored_l4(session_id: str) -> set[tuple[str, str]]:
    """Return {(agent, eval_name)} of L4 evals already written for this session.

    Used to make the judge idempotent so the daemon-thread pass and the synchronous
    end-of-session backstop never double-write. Empty set on any error (fail open: a
    missed dedup only risks a duplicate, never a missing score).
    """
    try:
        import os
        from core.db import get_client
        tenant_id = os.environ.get("TENANT_ID", "")
        if not tenant_id:
            return set()
        rows = (
            get_client().table("ag_evals")
            .select("agent, eval_name")
            .eq("tenant_id", tenant_id)
            .eq("session_id", session_id)
            .eq("layer", 4)
            .execute()
            .data
        )
        return {(r.get("agent"), r.get("eval_name")) for r in rows}
    except Exception:
        return set()


def _fetch_criteria(agent_names: list[str]) -> dict[str, list[dict]]:
    """
    Fetch enabled semantic eval configs via Argus ingest API.
    Returns {agent_name: [config_row, ...]}. Wildcard agent='*' applies to all agents.
    """
    if not agent_names:
        return {}
    try:
        from trace.logger import _ingest_get
        data = _ingest_get("/api/ingest/eval/configs", {})
        rows = data.get("configs", [])
    except Exception:
        return {}

    agent_set = set(agent_names)
    by_agent: dict[str, list[dict]] = {}
    for row in rows:
        # The judge scores only L4 (semantic) criteria. L3 rule checks are deterministic
        # and must not be run through the LLM — doing so (with their null prompt) scored
        # them against the bare eval name and produced meaningless 0% results (e.g. the
        # orchestrator's decision_made / exit_quality). Layer absent → keep (backward compat).
        layer = row.get("layer")
        if layer is not None:
            try:
                if int(layer) != 4:
                    continue
            except (TypeError, ValueError):
                pass
        agent = row.get("agent")
        if agent == "*":
            for name in agent_names:
                by_agent.setdefault(name, []).append(row)
        elif agent and agent in agent_set:
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
    """Write eval results via ingest API — auto-creates incidents for failed evals."""
    from trace.logger import _ingest_post
    for r in results:
        _ingest_post("/api/ingest/eval", {
            "session_id": session_id,
            "eval_name":  r["eval_name"],
            "agent":      agent,
            "layer":      4,
            "score":      r["score"],
            "passed":     r["passed"],
            "threshold":  r["threshold"],
            "detail":     {"reasoning": r["reasoning"]},
        })


def _patch_session_quality_score(session_id: str, all_results: dict[str, list[dict]]) -> None:
    """Write avg L4 quality score back to the session via Argus ingest PATCH."""
    scores = [
        r["score"]
        for results in all_results.values()
        for r in results
        if r.get("score") is not None
    ]
    if not scores:
        return
    avg_score = round(sum(scores) / len(scores), 4)
    try:
        from trace.logger import _ingest_patch
        _ingest_patch("/api/ingest/session", {
            "session_id":   session_id,
            "quality_score": avg_score,
        })
    except Exception as exc:
        print(f"[judge] quality_score patch failed for {session_id}: {exc}")


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
      Callers are responsible for pre-processing outputs so key fields are within the first
      3000 chars (see _prepare_scanner_for_judge, _prepare_risk_for_judge in orchestrator.py).

    Returns: {agent_name: [{eval_name, score, passed, threshold, reasoning}]}
    Empty dict if no criteria are configured or TENANT_ID is unset.
    """
    if not agent_outputs:
        return {}

    criteria_by_agent = _fetch_criteria(list(agent_outputs.keys()))
    if not criteria_by_agent:
        return {}

    # Idempotency: skip (agent, eval) already scored for this session. The judge can run
    # twice for one session — the orchestrator fires it in a daemon thread for a head start,
    # and the session fires a synchronous backstop at the end so evals still land if the
    # Action process exits before the daemon finishes. Without this, the two passes would
    # double-write (the ingest /eval route does not dedup).
    already = _already_scored_l4(session_id)

    client = anthropic.Anthropic()
    all_results: dict[str, list[dict]] = {}

    for agent, criteria in criteria_by_agent.items():
        output = agent_outputs.get(agent, "")
        if not output or not criteria:
            continue

        agent_results: list[dict] = []
        for criterion in criteria:
            if (agent, criterion.get("eval_name")) in already:
                continue
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
                # Incidents created by ingest /eval route automatically when passed=False
            except Exception as exc:
                print(f"[judge] eval write failed for {agent}: {exc}")
            all_results[agent] = agent_results

    if all_results:
        try:
            _patch_session_quality_score(session_id, all_results)
        except Exception as exc:
            print(f"[judge] quality_score patch failed: {exc}")

    return all_results


def evaluate_session_from_traces(session_id: str) -> None:
    """
    Read agent reasoning from traces via Argus ingest API and run LLM judge.
    Uses payload->agent_reasoning as judge input. Call after close_session().
    Non-blocking — logs failures, does not raise.
    """
    try:
        from trace.logger import _ingest_get
        data = _ingest_get("/api/ingest/trace", {
            "session_id": session_id,
            "step_type":  "agent_message",
            "limit":      "200",
        })
        rows = data.get("traces", [])
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

        # Scanner output is typically 3,500+ chars due to the full candidates list.
        # The judge window is 3,000 chars, so regime/scan_rationale/dropped_count
        # (which appear after the candidates array) get truncated and the judge
        # incorrectly reports them as missing. Re-order to put summary fields first,
        # matching what _prepare_scanner_for_judge does in the live orchestrator path.
        if "scanner" in combined:
            try:
                raw = combined["scanner"].strip()
                # Strip markdown code fences if present (LLM often wraps JSON in ```json ... ```)
                if raw.startswith("```"):
                    raw = raw.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
                scanner_data = json.loads(raw)
                candidates = scanner_data.get("candidates") or []
                combined["scanner"] = json.dumps({
                    "regime":         scanner_data.get("regime"),
                    "scan_rationale": scanner_data.get("scan_rationale"),
                    "dropped_count":  scanner_data.get("dropped_count", 0),
                    "n_returned":     scanner_data.get("n_returned", len(candidates)),
                    "top_candidates": candidates[:5],
                }, indent=2)
            except (json.JSONDecodeError, TypeError):
                pass  # leave raw if not valid JSON

        if combined:
            evaluate_session_outputs(session_id, combined)
    except Exception as exc:
        print(f"[judge] evaluate_session_from_traces failed: {exc}")
