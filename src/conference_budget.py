#!/usr/bin/env python3
"""Budget check for a conference participant: caps tool steps and distinct files read.

The conference is a quick analysis, not long work, and the limit must be a mechanism rather than a
prompt request. `src/hook.py` (already registered in the user's Claude Code settings) calls `check`
on every PreToolUse while the worker's `WS_CONFERENCE_BUDGET` is set. When the budget runs out the
tool call is denied with a reason that tells the agent to answer with what it already knows, so the
run still ends with an answer instead of being killed at a turn limit. The state lives in the file
named by `WS_CONFERENCE_BUDGET` (one per run, set by `conference_worker`); limits come from
`WS_CONFERENCE_MAX_STEPS` and `WS_CONFERENCE_MAX_FILES`.

Fail open on purpose: a missing env or a broken state file never blocks the run.
"""
import json
import os
import sys
from pathlib import Path

try:
    import fcntl
except ImportError:     # no fcntl (Windows): updates are not serialized
    fcntl = None

import conference      # noqa: E402  (stdlib-only at import time, by design)


def _limit(name, default):
    try:
        return max(0, int(os.environ.get(name, default)))
    except (TypeError, ValueError):
        return default


def _t(key, **kw):
    import i18n
    return i18n.t(key, **kw)


def _read_state(path):
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"steps": 0, "files": [], "ids": []}
    if not isinstance(data, dict):
        return {"steps": 0, "files": [], "ids": []}
    files = data.get("files")
    ids = data.get("ids")
    return {
        "steps": int(data.get("steps") or 0),
        "files": [f for f in files if isinstance(f, str)] if isinstance(files, list) else [],
        "ids": [i for i in ids if isinstance(i, str)][-50:] if isinstance(ids, list) else [],
    }


def check(data):
    """Denial reason when this tool call is past the budget, else None (allowed calls are counted).

    The file budget counts distinct Read paths, so re-reading a file is not charged twice; a new
    file is denied when the limit is already full. Every counted tool call charges one step.
    """
    if not isinstance(data, dict) or data.get("hook_event_name") != "PreToolUse":
        return None
    tool = data.get("tool_name")
    if tool not in conference.COUNTED_TOOLS:
        return None
    state_path = os.environ.get("WS_CONFERENCE_BUDGET")
    if not state_path:
        return None
    max_steps = _limit("WS_CONFERENCE_MAX_STEPS", conference.MAX_TOOL_STEPS)
    max_files = _limit("WS_CONFERENCE_MAX_FILES", conference.MAX_FILES_READ)
    with open(state_path + ".lock", "a") as lock:
        if fcntl is not None:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        state = _read_state(state_path)
        tool_id = data.get("tool_use_id")
        if isinstance(tool_id, str) and tool_id and tool_id in state["ids"]:
            return None      # the same event reaching the hook twice (e.g. the hook registered twice)
        if state["steps"] >= max_steps:
            return _t("conference.budget.steps", max_steps=max_steps)
        file_path = (data.get("tool_input") or {}).get("file_path")
        if tool == "Read" and isinstance(file_path, str) and file_path:
            if file_path not in state["files"]:
                if len(state["files"]) >= max_files:
                    return _t("conference.budget.files", max_files=max_files)
                state["files"].append(file_path)
        state["steps"] += 1
        if isinstance(tool_id, str) and tool_id:
            state["ids"] = (state["ids"] + [tool_id])[-50:]
        Path(state_path).write_text(json.dumps(state), encoding="utf-8")
    return None


def deny(reason):
    """PreToolUse hook output that blocks the tool and hands the reason to the agent."""
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": "deny",
        "permissionDecisionReason": reason,
    }}, ensure_ascii=False))


def main():
    try:
        reason = check(json.load(sys.stdin))
        if reason:
            deny(reason)
    except Exception:
        return      # fail open: the hook never breaks the run


if __name__ == "__main__":
    main()
