#!/usr/bin/env python3
"""Last permission mode per repo: recorded by the hook, applied when the repo's agent opens.

Claude Code records every mode change in the transcript as
`{"type": "permission-mode", "permissionMode": "<mode>"}`. The Stop/SessionEnd hook stores the last
one per repo; `opening_command` reads it and passes `--permission-mode` on the next launch. The mode is
only applied while the agent command is the same: a profile switch opens in the profile default
(decision D10). Every profile of the Claude harness opens in acceptEdits, the Claude default: a
custom Anthropic-compatible endpoint (such as `claude-deepseek`) is still the Claude harness and
never starts in bypass. OpenCode profiles do not call this module (they carry no Claude flags).
`auto` is only reapplied on the Claude harness.

This module is stdlib-only at import time: hook.py imports it on every Stop.
"""
import contextlib
import json
import os
from pathlib import Path

try:
    import fcntl
except ImportError:     # no fcntl (Windows): updates are not serialized
    fcntl = None

DEFAULT_MODE = "acceptEdits"            # every Claude harness profile starts here
MODES = {"default", "acceptEdits", "plan", "auto", "dontAsk", "bypassPermissions"}


def _state_dir():
    return Path(os.environ.get("WS_STATE", Path.home() / ".cache" / "workspace"))


def path():
    return _state_dir() / "permission.json"


def _lock_path():
    return _state_dir() / "permission.lock"


@contextlib.contextmanager
def _lock():
    """Exclusive lock shared by the hook and the launcher (several Stops may land together)."""
    _state_dir().mkdir(parents=True, exist_ok=True)
    with open(_lock_path(), "a") as lock:
        if fcntl is not None:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        yield


def read():
    """{repo: {"command": ..., "mode": ...}}; an unreadable file reads as empty."""
    try:
        data = json.loads(path().read_text(encoding="utf-8"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _write(data):
    path().parent.mkdir(parents=True, exist_ok=True)
    tmp = path().with_name(f"permission.json.{os.getpid()}.tmp")
    try:
        tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, path())
    finally:
        tmp.unlink(missing_ok=True)


def _repo(repo):
    return os.path.realpath(os.path.expanduser(str(repo or "")))


def _valid(mode):
    mode = str(mode or "")
    return mode if mode in MODES else DEFAULT_MODE


def _launch_command(key):
    """Command recorded by the workspace for this repo (`agent.json`), or "".

    A mode recorded by a session that was already running when the workspace started has no command
    in permission.json; taking it from agent.json lets the next opening apply it without waiting for
    a full profile switch."""
    try:
        data = json.loads((_state_dir() / "agent.json").read_text(encoding="utf-8"))
        entry = data.get(key)
        if isinstance(entry, dict):
            return str(entry.get("command") or "")
        return str(entry or "")
    except Exception:
        return ""


def record_mode(repo, mode):
    """Stores the last mode of a running session, keeping the command recorded at launch."""
    key = _repo(repo)
    if not key or not mode:
        return
    try:
        with _lock():
            data = read()
            entry = data.get(key)
            entry = dict(entry) if isinstance(entry, dict) else {}
            entry["mode"] = _valid(mode)
            if not entry.get("command"):
                entry["command"] = _launch_command(key)
            data[key] = entry
            _write(data)
    except Exception:
        pass


def record_launch(repo, command, mode):
    """Records the command that opened the repo and the mode it was opened with."""
    key = _repo(repo)
    if not key:
        return
    try:
        with _lock():
            data = read()
            data[key] = {"command": str(command), "mode": _valid(mode)}
            _write(data)
    except Exception:
        pass


def mode_for(repo, command, claude_harness=True):
    """Mode to launch: the last one for the same command, else acceptEdits.

    A different command means a profile switch, so the saved mode is not applied on that first
    opening. Every Claude harness profile (including `claude-deepseek`) defaults to acceptEdits and
    never starts in bypass. `auto` is only reapplied on the Claude harness; OpenCode profiles never
    reach this function."""
    default = DEFAULT_MODE
    try:
        entry = read().get(_repo(repo))
        if not isinstance(entry, dict) or entry.get("command") != command:
            return default
        mode = _valid(entry.get("mode"))
        return mode if claude_harness or mode != "auto" else default
    except Exception:
        return default
