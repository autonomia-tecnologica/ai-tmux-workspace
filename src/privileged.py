#!/usr/bin/env python3
"""Administrative commands (backlog item 154): the person approves on a separate tmux screen, then pkexec.

The policy engine decides `privileged` for an admin program; this module runs it. The whole command
line is shown with the question, and nothing runs unless the person answers yes on that screen. The
command then runs through pkexec, which asks the system for the second approval. The output and the
exit code go back to the caller as the result of the same call. Every run is appended to the policy
log (`policy.record`).
"""
import hashlib
import os
import shlex
import time

import i18n
import policy
import tmux

ELEVATORS = {"sudo", "doas"}     # a leading elevator word is replaced by pkexec; other shapes keep the line
SHELL_MARKS = set(";&|()<>`$\n")
SHELL_TOOLS = {"bash", "shell", "execute"}


def pkexec_argv(command):
    """argv that runs the whole command line through pkexec.

    A single simple command runs as `pkexec <argv>`, with a leading elevator word replaced by pkexec
    (`sudo ls` -> `pkexec ls`). Anything else (shell operators, `NAME=value`, elevator flags) runs as
    `pkexec sh -c <line>`: an elevator word inside it then runs as root, which needs no password.
    """
    text = str(command or "").strip()
    try:
        argv = shlex.split(text)
    except ValueError:
        argv = []
    simple = bool(argv) and not any(mark in text for mark in SHELL_MARKS) and "=" not in argv[0]
    if simple and os.path.basename(argv[0]) in ELEVATORS and len(argv) > 1 and not argv[1].startswith("-"):
        return ["pkexec", *argv[1:]]
    if simple:
        return ["pkexec", *argv]
    return ["pkexec", "sh", "-c", text]


def _audit_target(command):
    """Program name and a short hash of the whole command line: never the arguments (audit rule)."""
    if not command:
        return None
    try:
        words = shlex.split(command)
    except ValueError:
        words = []
    program = os.path.basename(words[0]) if words else "shell"
    digest = hashlib.sha256(command.encode("utf-8")).hexdigest()[:12]
    return f"{program} #{digest}"


def _command_of(event):
    tool_input = event.get("tool_input") if isinstance(event, dict) else None
    command = tool_input.get("command") if isinstance(tool_input, dict) else None
    return command if isinstance(command, str) else ""


def _cwd_of(event):
    value = event.get("cwd") if isinstance(event, dict) else None
    return value or os.environ.get("WS_SESSION_ROOT") or os.getcwd()


def _message(status, code, output, command):
    if status == "ran":
        body = output.strip() or i18n.t("privileged.result.empty")
        return i18n.t("privileged.result", code=code, output=body)
    if status == "no_tmux":
        return i18n.t("privileged.no_tmux", command=command)
    if status == "refused":
        return i18n.t("privileged.refused")
    if status == "timeout":
        return i18n.t("privileged.timeout")
    if status == "no_command":
        return i18n.t("privileged.not_shell")
    return i18n.t("privileged.interrupted")


def run_approved(event):
    """Asks the person and runs the command through pkexec. Returns a deny-shaped result.

    The result is `deny` on purpose: the command runs here, not in the agent's own tool, so the
    harness must not run it again. The message carries the output and exit code (or the reason
    nothing ran) back to the agent as the result of this same call. Never raises.
    """
    command = _command_of(event)
    tool = str(event.get("tool_name") or "").lower() if isinstance(event, dict) else ""
    status, code, output = "no_command", None, ""
    try:
        if command.strip() and tool in SHELL_TOOLS:
            status, code, output = tmux.approved_run(
                pkexec_argv(command), cwd=_cwd_of(event),
                question=i18n.t("privileged.ask"), command=command)
    except Exception:
        status, code, output = "interrupted", None, ""
    message = _message(status, code, output, command)
    try:
        policy.record({
            "ts": time.time(),
            "role": os.environ.get("WS_SESSION_ROLE") or None,
            "session": (event.get("session_id") if isinstance(event, dict) else None) or None,
            "capability": "admin",
            "target": _audit_target(command),
            "decision": "privileged",
            "reason": status,
            "exit": code,
        })
    except Exception:
        pass
    return {"decision": "deny", "capability": "admin", "reason": "policy.reason.privileged",
            "message": message}
