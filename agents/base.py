from __future__ import annotations

import concurrent.futures
import json
import re
import time
from typing import Any, Callable

_TOOL_TIMEOUT_S = 25   # yfinance / slow external calls must return within this window


def _dispatch_with_timeout(
    dispatch: Callable[[str, dict], Any],
    name: str,
    inp: dict,
) -> Any:
    """
    Run a tool dispatch in a thread with a hard timeout.
    Returns {"error": "timeout"} if the call does not finish in _TOOL_TIMEOUT_S seconds.

    Uses a module-level executor (not a context manager) so shutdown(wait=True) is
    never called inline — the yfinance/Alpaca thread is left as a daemon and the
    caller gets the timeout result immediately without blocking.
    """
    future = _TOOL_EXECUTOR.submit(dispatch, name, inp)
    try:
        return future.result(timeout=_TOOL_TIMEOUT_S)
    except concurrent.futures.TimeoutError:
        return {"error": f"tool timeout after {_TOOL_TIMEOUT_S}s"}


# Module-level executor — threads are daemon threads so they don't block process exit.
# Never shut it down inline; let the OS clean up on process termination.
_TOOL_EXECUTOR = concurrent.futures.ThreadPoolExecutor(max_workers=8, thread_name_prefix="tool")

import anthropic

from trace.logger import TraceLogger


def run_tool_loop(
    client: anthropic.Anthropic,
    model: str,
    system: str,
    tools: list[dict],
    initial_message: str,
    dispatch: Callable[[str, dict], Any],
    tracer: TraceLogger,
    agent_name: str,
    max_turns: int = 15,
    wall_clock_timeout_s: int | None = None,
) -> str:
    """
    Drive a Claude tool-use loop until end_turn or max_turns.
    Returns final text content. Raises RuntimeError if limit exceeded.
    Every tool call and the final message are logged via tracer.
    wall_clock_timeout_s caps total elapsed time across all turns and tool calls.
    """
    messages: list[dict] = [{"role": "user", "content": initial_message}]
    loop_start = time.monotonic()

    for turn in range(max_turns):
        if wall_clock_timeout_s is not None:
            elapsed = time.monotonic() - loop_start
            if elapsed >= wall_clock_timeout_s:
                raise RuntimeError(
                    f"{agent_name}: wall-clock timeout after {elapsed:.0f}s "
                    f"(limit {wall_clock_timeout_s}s, turn {turn})"
                )
        t0 = time.monotonic()
        response = client.messages.create(
            model=model,
            max_tokens=2048,
            system=system,
            tools=tools,
            messages=messages,
        )
        api_ms = int((time.monotonic() - t0) * 1000)
        tracer.log_tokens(agent_name, response.usage)

        if response.stop_reason == "end_turn":
            text = next((b.text for b in response.content if hasattr(b, "text")), "")
            tracer.log_agent_message(
                agent_name, text, "completed",
                tokens_input=response.usage.input_tokens,
                tokens_output=response.usage.output_tokens,
                model=model,
                latency_ms=api_ms,
            )
            return text

        if response.stop_reason == "tool_use":
            tool_results: list[dict] = []
            for block in response.content:
                if block.type == "tool_use":
                    t1 = time.monotonic()
                    result = _dispatch_with_timeout(dispatch, block.name, block.input)
                    tool_ms = int((time.monotonic() - t1) * 1000)
                    tracer.log_tool_call(
                        agent_name, block.name, block.input, result,
                        latency_ms=tool_ms,
                    )
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": json.dumps(result),
                    })
            messages.append({"role": "assistant", "content": response.content})
            messages.append({"role": "user", "content": tool_results})
            continue

        # ⛔ ANY OTHER STOP REASON USED TO FALL THROUGH AND SPIN. Neither branch above fired, nothing
        # was appended to `messages`, and the next iteration sent a byte-identical request, which
        # produced a byte-identical response, up to max_turns. The agent then died with a generic
        # "hit the N-turn limit", which describes the symptom and hides the cause.
        #
        # It cost the Learning Agent every EOD run from 10 Aug 2026: four reads, then sixteen
        # identical no-progress turns, twenty Sonnet calls burned per night, and on 12 Aug it tripped
        # the account's API usage limit. The failure was in the traces the whole time and the error
        # message pointed at the loop rather than at `max_tokens`. argus#583.
        #
        # Failing here is strictly better: same outcome, nineteen fewer calls, and the reason is in
        # the message. `max_tokens` is the one worth naming, because the fix is a bigger budget or a
        # shorter answer rather than anything about tools.
        hint = (
            " (the model's answer was truncated: raise max_tokens or ask for a shorter response)"
            if response.stop_reason == "max_tokens" else ""
        )
        raise RuntimeError(
            f"{agent_name}: tool loop stopped on '{response.stop_reason}' at turn {turn + 1} "
            f"of {max_turns}, which the loop cannot act on{hint}"
        )

    raise RuntimeError(f"{agent_name}: tool loop hit {max_turns}-turn limit without end_turn")


def parse_json_response(text: str) -> dict:
    """Extract JSON from a Claude text response, handling markdown code fences."""
    text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    m = re.search(r"```(?:json)?\s*([\s\S]+?)```", text)
    if m:
        return json.loads(m.group(1).strip())
    raise ValueError(f"No valid JSON in response: {text[:200]}")
