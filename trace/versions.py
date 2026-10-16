"""Which instructions and which code each step ran with (argus#1735).

Provy can name a change as the cause of a failure only when the steps say what they ran with. On production 0 of 10,614 spans carried a `prompt_version`.
This module is the label, and it is only a label: it changes no prompt, no model and no order.

* An agent that calls a model is labelled with a short hash of its system prompt, so the label changes exactly when the prompt does.
* A step of plain code (the intraday entry summary, a decision) has no prompt. It is labelled `code-<sha>`: the commit this process runs.

⛔ NEVER LET A LABEL BREAK A TRADE. Everything here returns None on any failure, and the logger sends no label then. Provy is never in the trade path.
"""
from __future__ import annotations

import hashlib
import os
import subprocess
from typing import Optional

_PROMPTS: dict[str, str] = {}
_CODE: Optional[str] = None
_CODE_READ = False


def prompt_hash(*texts: str) -> str:
    """`p-` and ten hex characters of the SHA-256 of the prompt text. Whitespace is collapsed, so re-indenting is not a new version."""
    joined = "\n".join(" ".join(str(t).split()) for t in texts)
    return "p-" + hashlib.sha256(joined.encode("utf-8")).hexdigest()[:10]


def register_prompt(agent: str, *system_prompts: str) -> str:
    """Say once which prompt(s) an agent runs with. Called where the prompt constants are defined, at import."""
    v = prompt_hash(*system_prompts)
    _PROMPTS[agent] = v
    return v


def code_version() -> Optional[str]:
    """`code-<sha>` for the commit this process runs, from PROVY_CODE_VERSION or git. None when neither is available."""
    global _CODE, _CODE_READ
    if _CODE_READ:
        return _CODE
    _CODE_READ = True
    try:
        env = os.environ.get("PROVY_CODE_VERSION", "").strip()
        if env:
            _CODE = env
        else:
            here = os.path.dirname(os.path.abspath(__file__))
            out = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=here, capture_output=True, text=True, timeout=3)
            sha = out.stdout.strip() if out.returncode == 0 else ""
            _CODE = f"code-{sha}" if sha else None
    except Exception:
        _CODE = None
    return _CODE


def version_for(agent: str, base: str) -> Optional[str]:
    """The label for one agent step: its registered prompt (by exact name, then base name), else the code version."""
    try:
        return _PROMPTS.get(agent) or _PROMPTS.get(base) or code_version()
    except Exception:
        return None
