#!/usr/bin/env python3
"""Hook for a conference participant: caps tool steps and distinct files read, and restricts writes.

The conference is a quick analysis, not long work, and the limit must be a mechanism rather than a
prompt request. `src/hook.py` (already registered in the user's Claude Code settings) calls `check`
on every PreToolUse while the worker's `WS_CONFERENCE_BUDGET` is set. When the budget runs out the
tool call is denied with a reason that tells the agent to answer with what it already knows, so the
run still ends with an answer instead of being killed at a turn limit. The state lives in the file
named by `WS_CONFERENCE_BUDGET` (one per run, set by `conference_worker`); limits come from
`WS_CONFERENCE_MAX_STEPS` and `WS_CONFERENCE_MAX_FILES`.

A write is only allowed to the conference repo's own `BACKLOG.md` (`WS_CONFERENCE_REPO`, also set by
`conference_worker`); any other target is denied. That check does not depend on the budget file, so
a run whose budget state could not be created still cannot edit anything else.

Fail open on purpose for the budget: a missing env or a broken state file never blocks the run. The
write restriction is the exception: it fails closed whenever the repo is known.
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


def _write_target(data, repo):
    """Absolute path the tool is about to write, or None when the input has no usable path."""
    tool_input = data.get("tool_input") if isinstance(data, dict) else None
    if not isinstance(tool_input, dict):
        return None
    path = tool_input.get("file_path") or tool_input.get("notebook_path")
    if not isinstance(path, str) or not path:
        return None
    target = Path(path).expanduser()
    if not target.is_absolute():
        target = Path(repo).expanduser() / target
    try:
        return target.resolve()
    except OSError:
        return None


def _own_backlog(data, repo):
    """Whether the write targets exactly the conference repo's BACKLOG.md."""
    target = _write_target(data, repo)
    if target is None:
        return False
    try:
        expected = (Path(repo).expanduser() / "BACKLOG.md").resolve()
    except OSError:
        return False
    return target == expected


def check(data):
    """Denial reason when this tool call is denied, else None (allowed calls are counted).

    A write is only allowed to the conference repo's BACKLOG.md. The file budget counts distinct
    Read paths, so re-reading a file is not charged twice; a new file is denied when the limit is
    already full. Every counted tool call charges one step, writes included.
    """
    if not isinstance(data, dict) or data.get("hook_event_name") != "PreToolUse":
        return None
    tool = data.get("tool_name")
    repo = os.environ.get("WS_CONFERENCE_REPO")
    if tool in conference.WRITE_TOOLS and repo and not _own_backlog(data, repo):
        return _t("conference.write_denied")
    if tool not in conference.COUNTED_TOOLS and tool not in conference.WRITE_TOOLS:
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
