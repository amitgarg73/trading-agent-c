from __future__ import annotations

import os
import urllib.request


_NTFY_SERVER = os.getenv("NTFY_SERVER", "https://ntfy.sh")


def send_alert(subject: str, body: str) -> bool:
    """POST to ntfy.sh topic. Returns True on delivery, False on any failure."""
    topic = os.getenv("NTFY_TOPIC_C")
    if not topic:
        return False

    try:
        req = urllib.request.Request(
            f"{_NTFY_SERVER}/{topic}",
            data=body.encode("utf-8"),
            headers={
                "Title":        subject,
                "Priority":     "high",
                "Content-Type": "text/plain",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status == 200
    except Exception:
        return False
