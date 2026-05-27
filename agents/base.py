from __future__ import annotations

import json
import re
import time
from typing import Any, Callable

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
) -> str:
    """
    Drive a Claude tool-use loop until end_turn or max_turns.
    Returns final text content. Raises RuntimeError if limit exceeded.
    Every tool call and the final message are logged via tracer.
    """
    messages: list[dict] = [{"role": "user", "content": initial_message}]

    for _ in range(max_turns):
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
                    result = dispatch(block.name, block.input)
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
