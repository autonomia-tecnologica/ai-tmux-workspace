#!/usr/bin/env python3
"""Administrative commands (backlog items 154 and 159): the person approves in a desktop window, then pkexec.

The policy engine decides `privileged` for an admin program; this module runs it. The approval is a
graphical question (kdialog or zenity) that shows the whole command line; nothing runs unless the
person answers yes there. The command then runs through pkexec, which asks the system (polkit agent)
for the second approval. The output and the exit code go back to the caller as the result of the
same call. No tmux is involved. Every run is appended to the policy log (`policy.record`).
"""
import hashlib
import html
import os
import shlex
import shutil
import subprocess
import time

import i18n
import policy

ELEVATORS = {"sudo", "doas"}     # a leading elevator word is replaced by pkexec; other shapes keep the line
SHELL_MARKS = set(";&|()<>`$\n")
SHELL_TOOLS = {"bash", "shell", "execute"}
APPROVAL_SECONDS = 120           # how long the person has to answer before nothing runs
MAX_OUTPUT = 20000               # characters of the command output sent back to the agent
WINDOW_TITLE = "Claude/OpenCode"
WINDOW_TOOLS = ("kdialog", "zenity")   # first one found wins
PASSWORD_CODES = (126, 127)      # pkexec: password not given or not authorized, or program not found
TRACE_REASON = {"password": "refused"}  # statuses whose audit reason differs from the status


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


def window_tool():
    """(name, path) of the dialog tool to ask with, or None when there is no desktop or no tool.

    The desktop is DISPLAY or WAYLAND_DISPLAY in the hook's environment; kdialog is preferred."""
    if not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
        return None
    for name in WINDOW_TOOLS:
        path = shutil.which(name)
        if path:
            return name, path
    return None


def _window_argv(tool, text):
    """argv of the question window. The text goes as one argument: never through a shell, never read
    as an option (`--text=`) and never as markup (zenity --no-markup; kdialog gets it escaped)."""
    name, path = tool
    if name == "kdialog":
        return [path, "--title", WINDOW_TITLE, "--yesno",
                html.escape(text).replace("\n", "<br>")]
    return [path, "--question", "--title=" + WINDOW_TITLE, "--no-markup", "--width=520",
            "--ok-label=" + i18n.t("privileged.window.yes"),
            "--cancel-label=" + i18n.t("privileged.window.no"),
            "--text=" + text]


def ask_window(tool, text, seconds=None):
    """Shows the question and waits. Returns "yes", "no" (No or window closed) or "timeout".

    Raises OSError when the tool cannot start. A timeout kills the window, so nothing stays open."""
    seconds = APPROVAL_SECONDS if seconds is None else seconds
    try:
        done = subprocess.run(_window_argv(tool, text), stdin=subprocess.DEVNULL,
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=seconds)
    except subprocess.TimeoutExpired:
        return "timeout"
    return "yes" if done.returncode == 0 else "no"


def run_pkexec(argv, cwd):
    """Runs argv (already through pkexec) with stdout and stderr captured. Returns (exit code, output).

    No timeout here: the person may still be typing the password in the system's own dialog."""
    done = subprocess.run(argv, cwd=cwd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                          stderr=subprocess.STDOUT, text=True, errors="replace")
    return done.returncode, done.stdout or ""


def approve_and_run(argv, cwd=None, command=""):
    """Asks the person in a desktop window, then runs `argv` (through pkexec) if they say yes.

    Returns (status, code, output). `status` is `ran` (code and output are set), `refused` (No or
    the window closed), `timeout`, `no_gui` (no desktop or no window tool: nothing ran), `password`
    (pkexec exit 126/127: nothing ran), `no_command` or `interrupted` (the run failed to start).
    The window shows the whole command line; `argv` is what runs and is never run without a yes.
    """
    argv = [str(arg) for arg in argv]
    if not argv:
        return "no_command", None, ""
    tool = window_tool()
    if tool is None:
        return "no_gui", None, ""
    try:
        answer = ask_window(tool, i18n.t("privileged.ask", command=command or shlex.join(argv)))
    except OSError:
        return "no_gui", None, ""
    if answer == "timeout":
        return "timeout", None, ""
    if answer != "yes":
        return "refused", None, ""
    try:
        code, output = run_pkexec(argv, str(cwd or os.getcwd()))
    except OSError:
        return "interrupted", None, ""
    if code in PASSWORD_CODES:
        return "password", code, ""
    return "ran", code, output[:MAX_OUTPUT]


def _message(status, code, output, command):
    if status == "ran":
        body = output.strip() or i18n.t("privileged.result.empty")
        return i18n.t("privileged.result", code=code, output=body)
    if status == "no_gui":
        return i18n.t("privileged.no_gui", command=command)
    if status == "refused":
        return i18n.t("privileged.refused")
    if status == "timeout":
        return i18n.t("privileged.timeout")
    if status == "password":
        return i18n.t("privileged.password", code=code)
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
            status, code, output = approve_and_run(pkexec_argv(command), cwd=_cwd_of(event),
                                                   command=command)
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
            "reason": TRACE_REASON.get(status, status),
            "exit": code,
        })
    except Exception:
        pass
    return {"decision": "deny", "capability": "admin", "reason": "policy.reason.privileged",
            "message": message}
