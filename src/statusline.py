#!/usr/bin/env python3
"""Claude Code statusLine command: keeps the account limits and each session's status in local files.

Claude Code pipes a JSON to stdin after each response. We keep:
- rate_limits (claude.ai plans: 5-hour and 7-day windows) in $WS_STATE/limits.json;
- model, context usage and cost of the session in $WS_STATE/sessions/<session_id>.status.json,
  read by statusbar.py and panel_sessions.py (skipped when the JSON has no session_id).

Standalone on purpose: no import of lib, because it runs several times per response. Every field
is read defensively (the JSON varies between Claude Code versions). No network, no credentials.
Prints nothing, so the Claude Code status line itself stays empty.
"""
import json
import os
import re
import sys
import time
from pathlib import Path

STATE = Path(os.environ.get("WS_STATE", Path.home() / ".cache" / "workspace"))
SESSION_ID = re.compile(r"[A-Za-z0-9_-]{1,128}")


def _dig(data, *path):
    for key in path:
        if not isinstance(data, dict):
            return None
        data = data.get(key)
    return data


def _number(value):
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _text(value):
    if not isinstance(value, str) or not value.strip():
        return None
    return value.strip()[:60]


def _write_json(path, data):
    """Atomic write: a reader never sees a half-written file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(data))
    os.replace(tmp, path)


def _main(data):
    limits = data.get("rate_limits")
    if isinstance(limits, dict):
        _write_json(STATE / "limits.json", {"ts": time.time(), "rate_limits": limits})

    session_id = data.get("session_id")
    if isinstance(session_id, str) and SESSION_ID.fullmatch(session_id):
        _write_json(STATE / "sessions" / f"{session_id}.status.json", {
            "ts": time.time(),
            "model": _text(_dig(data, "model", "display_name")),
            "context_pct": _number(_dig(data, "context_window", "used_percentage")),
            "cost_usd": _number(_dig(data, "cost", "total_cost_usd")),
        })


def main():
    try:
        data = json.load(sys.stdin)
        if isinstance(data, dict):
            _main(data)
    except Exception:
        pass


if __name__ == "__main__":
    main()
