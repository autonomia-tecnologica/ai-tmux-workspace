#!/usr/bin/env python3
"""Claude Code hook: records the session state for the repo panel's dot.

Reads the hook JSON from stdin and writes ~/.cache/workspace/sessions/<session>.json. On PreToolUse
it also asks the central capability policy (`src/policy.py`) for a decision when the session has a
declared role (`WS_SESSION_ROLE`); without a role the policy stays off, so sessions opened before
the launcher sets it keep working. With a role, an engine failure is a denial (fail closed)."""
import contextlib
import json
import os
import sys
import time
from pathlib import Path

try:
    import fcntl
except ImportError:     # no fcntl (Windows): updates are not serialized
    fcntl = None

sys.path.insert(0, str(Path(__file__).resolve().parent))

import yaml_min  # noqa: E402  (stdlib-only, no lib import)

try:
    import permission_state  # noqa: E402  (stdlib-only, same reason)
except Exception:
    permission_state = None

SESSIONS = Path(os.environ.get("WS_STATE", Path.home() / ".cache" / "workspace")) / "sessions"

# Deliberately does not import `lib`: this hook runs on every Claude Code event, so it must stay
# light and never fail on import. That is why the settings read in _notifications_enabled
# duplicates lib's config loader instead of calling it. Keep it that way.

EVENTS = {
    "SessionStart", "SessionEnd", "UserPromptSubmit", "PreToolUse", "PostToolUse",
    "PostToolUseFailure", "PermissionRequest", "PermissionDenied", "Notification", "Elicitation",
    "Stop", "StopFailure", "SubagentStart", "SubagentStop", "CwdChanged", "PostModelSwitch",
}
CLAUDE_NAMES = ("claude", "claude.exe")
MAX_LAST_MESSAGE = 500
MAX_PARENT_HOPS = 32


def _one_line(text, n=100):
    text = " ".join(str(text or "").split())
    return text if len(text) <= n else text[: n - 1].rstrip() + "…"


# Reads the config itself rather than through lib; see the note near the top of this file.
# Same path rules as lib.read_config: config.yaml, or the old settings.json while it does not exist.
def _notifications_enabled():
    try:
        if "AT_WS_CONFIG" in os.environ:
            cfg = Path(os.environ["AT_WS_CONFIG"])
            legacy = cfg.with_suffix(".json")
        else:
            folder = Path.home() / ".config" / "ai-tmux-workspace"
            cfg, legacy = folder / "config.yaml", folder / "settings.json"
        file = cfg if cfg.exists() else legacy
        if file.suffix == ".json":
            data = json.loads(file.read_text(encoding="utf-8"))
        else:
            data = yaml_min.loads(file.read_text(encoding="utf-8")) or {}
        return data.get("notifications", True) is not False
    except Exception:
        return True


def _last_assistant_text(d):
    """End of the last assistant message: payload field, else the tail of the transcript."""
    if d.get("last_assistant_message"):
        return str(d["last_assistant_message"])
    try:
        with open(d["transcript_path"], "rb") as f:
            f.seek(0, os.SEEK_END)
            f.seek(max(0, f.tell() - 262144))
            lines = f.read().decode("utf-8", "replace").splitlines()
        for line in reversed(lines):
            try:
                m = json.loads(line)
            except ValueError:
                continue
            if m.get("type") != "assistant":
                continue
            content = (m.get("message") or {}).get("content")
            if isinstance(content, str):
                return content
            texts = [c.get("text", "") for c in content or [] if isinstance(c, dict) and c.get("type") == "text"]
            if any(t.strip() for t in texts):
                return "\n".join(texts)
    except Exception:
        pass
    return ""


def _last_permission_mode(d):
    """Last permission mode recorded in the transcript, or "".

    Claude Code writes {"type": "permission-mode", "permissionMode": "auto"} when the person changes
    the mode; the launcher reapplies it on the next opening while the agent command is the same."""
    try:
        with open(d["transcript_path"], "rb") as f:
            f.seek(0, os.SEEK_END)
            f.seek(max(0, f.tell() - 262144))
            lines = f.read().decode("utf-8", "replace").splitlines()
    except Exception:
        return ""
    for line in reversed(lines):
        try:
            m = json.loads(line)
        except ValueError:
            continue
        if m.get("type") == "permission-mode":
            mode = m.get("permissionMode")
            return str(mode) if mode else ""
    return ""


def _record_tab_session(d):
    """Writes the Claude session id to the tab that launched it, so the workspace can reopen it.

    Only when the launcher exports `WS_SESSION_TAB`; without it (a session opened by hand) the
    file is left alone. Path format is shared with `session_state.py`."""
    tab = os.environ.get("WS_SESSION_TAB", "")
    if not tab:
        return
    sid = d.get("session_id")
    if not sid:
        return
    base = Path(os.environ.get("WS_STATE", Path.home() / ".cache" / "workspace")) / "tabs"
    path = base / f"session-{tab}"
    text = str(sid)
    try:
        base.mkdir(parents=True, exist_ok=True)
        if path.exists() and path.read_text() == text:
            return
        path.write_text(text)
    except OSError:
        pass


def _record_permission_mode(d):
    if permission_state is None:
        return
    mode = _last_permission_mode(d)
    if mode:
        try:
            permission_state.record_mode(d.get("cwd") or "", mode,
                                         profile=os.environ.get("WS_SESSION_PROFILE", ""))
        except Exception:
            pass


def _snippet(d, ev):
    try:
        if ev in ("Notification", "Elicitation"):
            return _one_line(d.get("message"))
        if ev in ("Stop", "StopFailure"):
            return _one_line(_last_assistant_text(d)[-300:])
        ti = d.get("tool_input") or {}
        detail = ti.get("command") or ti.get("file_path") or ti.get("question") or ""
        return _one_line(f"{d.get('tool_name') or ''} {detail}".strip())
    except Exception:
        return ""


def _notify(d, ev):
    if not _notifications_enabled():
        return
    from notify import notify
    notify(os.path.basename((d.get("cwd") or "").rstrip("/\\")) or "Claude Code", _snippet(d, ev))


# --- claude process lookup -------------------------------------------------------------------

def _proc_stat(pid):
    """(ppid, comm) from /proc/<pid>/stat. Raises OSError when /proc is missing (e.g. macOS)."""
    with open(f"/proc/{pid}/stat", "rb") as f:
        raw = f.read().decode("utf-8", "replace")
    comm = raw[raw.index("(") + 1: raw.rindex(")")]
    ppid = int(raw[raw.rindex(")") + 2:].split()[1])
    return ppid, comm


def _is_claude(pid, comm):
    if comm == "claude":
        return True
    # The npm install runs as `node .../claude-code/cli.js`, so look at the first two argv entries.
    with open(f"/proc/{pid}/cmdline", "rb") as f:
        argv = [a.decode("utf-8", "replace") for a in f.read().split(b"\0")[:2]]
    return any(os.path.basename(a) in CLAUDE_NAMES for a in argv) or any("claude-code" in a for a in argv[1:])


def _claude_pid():
    """PID of the claude process above this hook (hooks run through `sh -c`); 0 when not found."""
    pid = os.getppid()
    for _ in range(MAX_PARENT_HOPS):
        if pid <= 1:
            break
        try:
            ppid, comm = _proc_stat(pid)
            if _is_claude(pid, comm):
                return pid
        except (OSError, ValueError, IndexError):
            return 0
        pid = ppid
    return 0


def _alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:     # exists but not signalable (EPERM): treat as alive
        return True
    return True


# --- session files: lock, read, atomic write ---------------------------------------------------

def _paths(sid):
    return SESSIONS / f"{sid}.json", SESSIONS / f"{sid}.lock"


@contextlib.contextmanager
def _session_lock(sid):
    """Exclusive per-session lock held for a read-modify-write. The lock file is separate from the
    JSON so the JSON can be replaced atomically while the lock is held."""
    SESSIONS.mkdir(parents=True, exist_ok=True)
    with open(_paths(sid)[1], "a") as lock:
        if fcntl is not None:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        yield    # closing the file releases the lock


def _read_state(path):
    try:
        data = json.loads(path.read_text())
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _write_state(path, state):
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    try:
        tmp.write_text(json.dumps(state))
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


# --- state machine -------------------------------------------------------------------------

def _has_background_work(d):
    return bool(d.get("background_tasks")) or bool(d.get("session_crons"))


def _apply(e, ev, d, ctx):
    """Applies one event to the session state `e`, in place. Returns False when nothing is written."""
    e.update(cwd=d.get("cwd", e.get("cwd", "")), ts=ctx["now"], pane=ctx["pane"],
             tmux_socket=ctx["socket"], claude_pid=ctx["claude_pid"],
             profile=os.environ.get("WS_SESSION_PROFILE", ""))
    if d.get("transcript_path"):
        e["transcript_path"] = str(d["transcript_path"])
    e["agents"] = list(e.get("agents") or [])
    e["agent_types"] = dict(e.get("agent_types") or {})

    if ev in ("SubagentStart", "SubagentStop"):
        # running subagents are tracked in e["agents"]; the session color is derived from it
        agent_id = d.get("agent_id")
        if ev == "SubagentStart" and agent_id and agent_id not in e["agents"]:
            e["agents"].append(agent_id)
            e["agent_types"][agent_id] = d.get("agent_type") or "agent"
        elif ev == "SubagentStop":
            e["agents"] = [a for a in e["agents"] if a != agent_id]
        e["agent_types"] = {k: v for k, v in e["agent_types"].items() if k in e["agents"]}
        return True
    if ev == "SessionStart":
        if d.get("source") != "compact":     # compact keeps the session: only ts and context fields move
            e.update(state="idle", tool=None, agents=[], agent_types={})
        return True
    if ev == "UserPromptSubmit":     # a new prompt starts clean: a lost SubagentStop cannot stick
        e.update(state="working", tool=None, stopped=False, error=False, agents=[], agent_types={})
        return True
    if ev in ("Stop", "StopFailure"):
        if _has_background_work(d):    # background tasks or crons keep the session working
            e.update(state="working", tool=None, stopped=False)
        else:                          # turn ended with text in the session: red until the next prompt
            e.update(state="question", tool=None, stopped=True)
        e["error"] = ev == "StopFailure"
        if "last_assistant_message" in d:
            e["last_assistant_message"] = str(d["last_assistant_message"] or "")[:MAX_LAST_MESSAGE]
        return True
    if ev in ("PostModelSwitch", "CwdChanged"):    # only ts (and cwd, set above) move
        return True
    if ev == "PermissionDenied":
        e.update(state="working", tool=None, stopped=False)
        return True
    if ev == "Elicitation":          # the session waits on the person: the existing "question" state
        e.update(state="question", stopped=False)
        return True
    if ev == "PreToolUse" and d.get("tool_name") in ("AskUserQuestion", "ExitPlanMode"):
        e.update(state="question", tool=None, stopped=False)    # poll/plan approval: waiting on the person
        return True
    if d.get("agent_id") and ev in ("PreToolUse", "PostToolUse", "PostToolUseFailure"):
        # subagent tools share the parent's session id: the session is waiting on an agent
        # (green), not on a long-running command (yellow), and must not mask a pending question.
        if e.get("state") != "question":
            e.update(state="working", tool=None)
        return True
    if ev == "PreToolUse":
        e.update(state="working", tool=d.get("tool_name"), tool_ts=ctx["now"])
        return True
    if ev in ("PostToolUse", "PostToolUseFailure"):
        e.update(state="working", tool=None)
        return True
    if ev == "PermissionRequest" or (ev == "Notification" and d.get("notification_type")
                                     in ("permission_prompt", "elicitation_dialog")):
        e.update(state="question", stopped=False)
        return True
    return False    # idle_prompt keeps whatever Stop set; unknown events change nothing


def _record(d, ev, sid):
    """Applies the event to the session file under its lock. Returns the context used."""
    ctx = {
        "now": time.time(),
        "pane": os.environ.get("TMUX_PANE"),
        "socket": os.environ.get("TMUX", "").split(",")[0],
        "claude_pid": _claude_pid(),
    }
    path = _paths(sid)[0]
    with _session_lock(sid):
        e = _read_state(path)
        prev = e.get("state")
        written = _apply(e, ev, d, ctx)
        if written:
            _write_state(path, e)
    if written and e.get("state") == "question" and prev != "question":
        try:    # only the transition into red: PermissionRequest + Notification fire once
            _notify(d, ev)
        except Exception:
            pass
    return ctx


def _is_stale(state, ctx):
    """True when a session file belongs to this pane and tmux server and its claude process is gone.
    A file written by the same claude process is also stale: /clear reuses the process with a new id."""
    if state.get("pane") != ctx["pane"] or state.get("tmux_socket", "") != ctx["socket"]:
        return False
    other_pid = int(state.get("claude_pid") or 0)
    if other_pid and other_pid != ctx["claude_pid"] and _alive(other_pid):
        return False    # live session (e.g. an outer claude around a nested `claude -p`)
    return True


def _sweep_stale(sid, ctx):
    """On SessionStart, removes older session files of this pane whose claude process is gone."""
    keep = _paths(sid)[0].name
    for other in SESSIONS.glob("*.json"):
        other_sid = other.name[: -len(".json")]
        if other.name == keep or "." in other_sid:    # "<sid>.status.json" belongs to statusline
            continue
        try:
            if not _is_stale(_read_state(other), ctx):    # unlocked pre-check, re-checked under the lock
                continue
            with _session_lock(other_sid):
                other_state = _read_state(other)
                if _is_stale(other_state, ctx):
                    other.unlink(missing_ok=True)
                    _paths(other_sid)[1].unlink(missing_ok=True)
        except Exception:
            pass


def _end_session(sid):
    path, lock = _paths(sid)
    with _session_lock(sid):
        path.unlink(missing_ok=True)
        lock.unlink(missing_ok=True)


def _valid_session_id(sid):
    return isinstance(sid, str) and bool(sid) and sid not in (".", "..") and not any(c in sid for c in "/\\\0")


def _conference_budget(d):
    """PreToolUse of a conference run: its write restriction and tool/file budget, both set only by
    the worker's environment. The write restriction must hold even when the budget file is missing.
    Returns True when it emitted a denial, so the policy does not answer the same event twice."""
    if not os.environ.get("WS_CONFERENCE_BUDGET") and not os.environ.get("WS_CONFERENCE_REPO"):
        return False
    try:
        import conference_budget
        reason = conference_budget.check(d)
    except Exception:
        return False    # fail open, like everything else in this hook
    if reason:
        conference_budget.deny(reason)
        return True
    return False


def _policy_message(result):
    """Human text for the decision. The engine returns an i18n key; a missing locale keeps the key."""
    try:
        import i18n
        return i18n.t(result.get("reason") or "policy.reason.error",
                      capability=result.get("capability") or "-")
    except Exception:
        return result.get("reason") or "policy.reason.error"


def _request_popup(d):
    """Receiver side of session requests: shows the approval popup for a pending request.

    Called on SessionStart and Stop, so the person decides in the receiving agent. The popup is
    detached: the hook never waits for it and never fails because of it."""
    if os.environ.get("WS_CONFERENCE_REPO") or os.environ.get("WS_CONFERENCE_BUDGET"):
        return
    try:
        import conference
        conference.spawn_approval(d.get("cwd") or os.getcwd())
    except Exception:
        pass


def _request_context(d):
    """Receiver side: hands an approved conversation request to the agent on its next prompt.

    Only approved requests reach the agent; a refused or expired one is never read. The text is
    handed over once (`delivered` in the request file)."""
    try:
        import conference
        text = conference.conversation_context(d.get("cwd") or os.getcwd())
    except Exception:
        return
    if not text:
        return
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "UserPromptSubmit",
        "additionalContext": text,
    }}, ensure_ascii=False))


def _policy_check(d):
    """PreToolUse decision from the central policy, the same engine the OpenCode adapter calls.

    No `WS_SESSION_ROLE`: the policy is inactive on purpose, so sessions from before the launcher
    declares a box are untouched. With a declared role, an engine failure becomes `deny`: the
    OpenCode adapter already fails closed, and the two harnesses must answer the same way. `allow`
    is emitted only for an `auto` upgrade, which is the one case the policy has to make effective; a
    plain allow lets the harness's own permission flow continue.
    """
    if not os.environ.get("WS_SESSION_ROLE"):
        return
    try:
        import policy
        result = policy.check_event(d)
    except Exception:
        result = {"decision": "deny", "capability": None, "reason": "policy.reason.error"}
    if not isinstance(result, dict):
        result = {"decision": "deny", "capability": None, "reason": "policy.reason.error"}
    decision = result.get("decision")
    if decision not in ("allow", "ask", "deny"):
        result = {"decision": "deny", "capability": None, "reason": "policy.reason.error"}
        decision = "deny"
    if decision == "allow" and result.get("reason") != "policy.reason.auto":
        return
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": decision,
        "permissionDecisionReason": _policy_message(result),
    }}, ensure_ascii=False))


def main():
    d = json.load(sys.stdin)
    if not isinstance(d, dict):
        return
    ev, sid = d.get("hook_event_name"), d.get("session_id")
    if not isinstance(ev, str) or ev not in EVENTS or not _valid_session_id(sid):
        return
    if ev == "PreToolUse":
        if not _conference_budget(d):
            _policy_check(d)
    if ev == "SessionEnd":
        _record_permission_mode(d)
        _record_tab_session(d)
        _end_session(sid)
        return
    ctx = _record(d, ev, sid)
    if ev == "SessionStart" and ctx["pane"]:
        _sweep_stale(sid, ctx)
    if ev == "Stop":
        _record_permission_mode(d)
        _record_tab_session(d)
    if ev == "UserPromptSubmit":
        _request_context(d)
    if ev in ("SessionStart", "Stop"):
        _request_popup(d)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass
