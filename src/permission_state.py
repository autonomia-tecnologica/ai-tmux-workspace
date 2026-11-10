#!/usr/bin/env python3
"""Last permission mode per repo and profile: recorded by the hook, applied when the repo's agent opens.

Claude Code records every mode change in the transcript as
`{"type": "permission-mode", "permissionMode": "<mode>"}`. The Stop/SessionEnd hook stores the last
one per repo and profile (`WS_SESSION_PROFILE`); `session_command` reads it and passes
`--permission-mode` on the next launch. The mode is only applied while the agent command is the same:
a profile switch opens in the profile default (decision D10). Every profile of the Claude harness
opens in acceptEdits, the Claude default: a custom Anthropic-compatible endpoint (such as
`claude-deepseek`) is still the Claude harness and never starts in bypass. OpenCode profiles do not
call this module (they carry no Claude flags). `auto` is only reapplied on the Claude harness.

The file format is `{repo: {profile: {"command": ..., "mode": ...}}}`, with the `claude` profile as
the default. The old format `{repo: {"command": ..., "mode": ...}}` is read as the `claude` profile.

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
    """{repo: {profile: {"command": ..., "mode": ...}}}; an unreadable file reads as empty."""
    try:
        data = json.loads(path().read_text(encoding="utf-8"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _profiles_of(entry):
    """Profile map of one repo entry in the new `{profile: {command, mode}}` shape.

    The old per-repo shape `{command, mode}` is read as the `claude` profile, so files written before
    item 97 keep working. A non-mapping entry (or a new-format entry whose values are not maps) reads
    as empty; each repo entry is judged on its own."""
    if not isinstance(entry, dict):
        return {}
    if "command" in entry or "mode" in entry:
        return {"claude": entry}
    return {str(profile): dict(value) for profile, value in entry.items() if isinstance(value, dict)}


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


def record_mode(repo, mode, profile=""):
    """Stores the last mode of a running session for a profile, keeping the command recorded at launch."""
    key = _repo(repo)
    if not key or not mode:
        return
    profile = str(profile or "claude")
    try:
        with _lock():
            data = read()
            profiles = _profiles_of(data.get(key))
            entry = profiles.get(profile)
            entry = dict(entry) if isinstance(entry, dict) else {}
            entry["mode"] = _valid(mode)
            if not entry.get("command"):
                entry["command"] = _launch_command(key)
            profiles[profile] = entry
            data[key] = profiles
            _write(data)
    except Exception:
        pass


def record_launch(repo, command, mode, profile=""):
    """Records the command that opened the repo and the mode it was opened with, for a profile."""
    key = _repo(repo)
    if not key:
        return
    profile = str(profile or "claude")
    try:
        with _lock():
            data = read()
            profiles = _profiles_of(data.get(key))
            profiles[profile] = {"command": str(command), "mode": _valid(mode)}
            data[key] = profiles
            _write(data)
    except Exception:
        pass


def mode_for(repo, command, claude_harness=True, profile=""):
    """Mode to launch: the last one for the same command and profile, else acceptEdits.

    A different command means a profile switch, so the saved mode is not applied on that first
    opening. Every Claude harness profile (including `claude-deepseek`) defaults to acceptEdits and
    never starts in bypass. `auto` is only reapplied on the Claude harness; OpenCode profiles never
    reach this function."""
    default = DEFAULT_MODE
    try:
        entry = _profiles_of(read().get(_repo(repo))).get(str(profile or "claude"))
        if not isinstance(entry, dict) or entry.get("command") != command:
            return default
        mode = _valid(entry.get("mode"))
        return mode if claude_harness or mode != "auto" else default
    except Exception:
        return default
