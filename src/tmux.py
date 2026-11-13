"""The only place that runs the tmux command. Panels and helpers call these functions instead.

run() never raises: tmux missing, a timeout or a failing command gives "" so a panel keeps drawing
while the server is busy or gone. Pass timeout=None for commands that block until the user closes
them (display-popup -E).
"""
import os
import shlex
import shutil
import subprocess
import tempfile

import i18n

TIMEOUT = 3
SH = shutil.which("sh") or "/bin/sh"


def run(*args, timeout=TIMEOUT):
    """Runs `tmux *args` and returns its stdout stripped ("" on any failure)."""
    try:
        r = subprocess.run(["tmux", *args], capture_output=True, text=True, timeout=timeout,
                           stdin=subprocess.DEVNULL)
        return r.stdout.strip()
    except Exception:
        return ""


_SHELLS = {"sh", "bash", "zsh", "fish", "dash", "ksh", "tcsh", "csh", "nu"}


def live_panes():
    """Set of pane ids alive now and not sitting at a bare shell prompt, or None when tmux cannot be
    queried (different from "no panes"). A pane back at a shell means Claude exited there, possibly
    without SessionEnd, so its session file is stale."""
    try:
        r = subprocess.run(["tmux", "list-panes", "-a", "-F", "#{pane_id} #{pane_current_command}"],
                           capture_output=True, text=True, timeout=TIMEOUT, stdin=subprocess.DEVNULL)
    except Exception:
        return None
    if r.returncode != 0:
        return None
    panes = set()
    for line in r.stdout.splitlines():
        pid, _, cmd = line.partition(" ")
        if pid and cmd.strip().lstrip("-") not in _SHELLS:
            panes.add(pid)
    return panes


def server_socket():
    """Socket path of the tmux server in use, "" when unknown. Same value the hook stores as
    tmux_socket (first field of $TMUX)."""
    sock = os.environ.get("TMUX", "").split(",")[0]
    return sock or run("display", "-p", "#{socket_path}")


def show_w(window, option):
    """Value of a window option (`show -wqv`), "" when unset."""
    return run("show", "-wqv", "-t", window, option)


def display(fmt, target=None):
    """Expands a format string (`display -p`), for the given target when one is passed."""
    args = ["display", "-p"]
    if target is not None:
        args += ["-t", target]
    return run(*args, fmt)


def message(text, ms=2000):
    """Shows text in the status line for `ms` milliseconds."""
    run("display-message", "-d", str(ms), text)


def popup(cmd, cwd, size="92%", client=None):
    """Opens `cmd` in a tmux popup at `size`x`size` in `cwd`, blocking until the user closes it.
    `client` targets a specific attached client; without it tmux uses the current one."""
    args = ["display-popup", "-E"]
    if client is not None:
        args += ["-c", client]
    args += ["-w", size, "-h", size, "-d", cwd, cmd]
    run(*args, timeout=None)


def menu(title, items):
    """Shows a tmux menu until a choice or Esc. `items` is [(label, key, command)], where command is
    a tmux command string run on selection (unique keys, e.g. "1", "2")."""
    args = ["display-menu", "-T", title.replace("#", "##")]
    for label, key, command in items:
        args += [label.replace("#", "##"), key, command]
    run(*args, timeout=None)


def quote_cmd(argv):
    """Quotes a command for a tmux command string: shell-quotes each argument, then wraps the whole
    line in double quotes, escaping \\ " $ and doubling # (tmux expands formats in it)."""
    line = " ".join(shlex.quote(str(a)) for a in argv)
    escaped = line.replace("\\", "\\\\").replace('"', '\\"').replace("$", "\\$").replace("#", "##")
    return '"' + escaped + '"'


def confirm_then(prompt, argv):
    """Asks `prompt` in the tmux status line (y/n) and, on yes, runs argv in the background."""
    run("confirm-before", "-p", prompt.replace("#", "##"), f"run-shell -b {quote_cmd(argv)}", timeout=20)


def clients():
    """Names of the clients attached to this tmux server, [] when not in tmux or none is attached."""
    if not os.environ.get("TMUX"):
        return []
    return [line for line in run("list-clients", "-F", "#{client_name}").splitlines() if line]


def _show_privileged(command):
    """Outside tmux: prints the full command and waits for an explicit confirmation. Never runs it:
    a privileged command only runs on its own tmux screen."""
    print(i18n.t("privileged.outside", command=command))
    try:
        answer = input(i18n.t("privileged.confirm"))
    except (EOFError, KeyboardInterrupt):
        answer = ""
    if answer.strip().lower() not in i18n.t("privileged.answer.yes").split():
        print(i18n.t("privileged.declined"))
        return None
    print(i18n.t("privileged.not_run"))
    return None


def _privileged_script(command, code_file):
    """Shell script run inside the popup: shows the full command before it runs, keeps the output
    on screen until Enter and records the exit code in `code_file`."""
    return "\n".join([
        "#!/bin/sh",
        "printf '%s\\n' " + shlex.quote(i18n.t("privileged.title")),
        "printf '  %s\\n\\n' " + shlex.quote(command),
        command,
        "code=$?",
        "printf '\\n%s %s\\n' " + shlex.quote(i18n.t("privileged.exit")) + ' "$code"',
        'printf "%s" "$code" > ' + shlex.quote(code_file),
        "printf '%s' " + shlex.quote(i18n.t("privileged.close")),
        "read -r _",
        'exit "$code"',
        "",
    ])


def _exit_code(path):
    try:
        with open(path, encoding="utf-8") as handle:
            return int(handle.read().strip())
    except (OSError, ValueError):
        return None


def privileged(argv, cwd=None):
    """Runs a command that needs root, only on its own screen (backlog item 61).

    Inside tmux with an attached client, opens a display-popup that prints the full command before
    anything runs (visible before the password prompt), keeps the output on screen until the person
    presses Enter and records the exit code in a temp file, returned here. Without an attached
    client, prints the command and waits for an explicit confirmation; nothing runs. Returns the
    exit code, or None when the command did not run.
    """
    argv = [str(arg) for arg in argv]
    if not argv:
        return None
    command = shlex.join(argv)
    attached = clients()
    if not attached:
        return _show_privileged(command)
    directory = tempfile.mkdtemp(prefix="ws-privileged-")
    code_file = os.path.join(directory, "exit")
    script = os.path.join(directory, "run.sh")
    try:
        with open(script, "w", encoding="utf-8") as handle:
            handle.write(_privileged_script(command, code_file))
        os.chmod(script, 0o700)
        popup(quote_cmd([SH, script]), str(cwd or os.getcwd()), client=attached[0])
        return _exit_code(code_file)
    finally:
        shutil.rmtree(directory, ignore_errors=True)


# --- approved administrative commands (backlog item 154) ----------------------------------------

BASH = shutil.which("bash") or "/bin/bash"
APPROVAL_SECONDS = 120      # how long the person has to answer before nothing runs
MAX_OUTPUT = 20000          # characters of the command output sent back to the agent


def _yes_pattern():
    """Shell case pattern for the affirmative answers (`privileged.answer.yes`), any case."""
    forms = []
    for word in i18n.t("privileged.answer.yes").split():
        for form in (word, word.upper(), word.capitalize()):
            if form not in forms:
                forms.append(form)
    return "|".join(shlex.quote(form) for form in forms)


def _approved_script(command, question, seconds, exec_line, code_file, output_file):
    """Bash script for the approval popup: the question and the full command, a yes/no answer with
    a time limit, then the command's output (tee'd to `output_file`) and its exit code.

    The code file holds the exit code, or `refused`/`timeout` when nothing ran, so the caller can
    tell a refusal from a failure. bash is required for `read -t` and PIPESTATUS.
    """
    def write_code(value):
        return "printf '%s' " + shlex.quote(value) + " > " + shlex.quote(code_file)

    def stop(message, value):
        return [write_code(value),
                "printf '\\n%s\\n' " + shlex.quote(message),
                "printf '%s' " + shlex.quote(i18n.t("privileged.close")),
                "read -r _",
                "exit 1"]

    lines = [
        "#!/bin/bash",
        "printf '%s\\n\\n' " + shlex.quote(question),
        "printf '%s\\n\\n' " + shlex.quote(i18n.t("privileged.ask.command", command=command)),
        "read -r -t " + str(int(seconds)) + " -p " + shlex.quote(i18n.t("privileged.ask.answer"))
        + " answer",
        "status=$?",
        'if [ "$status" -ne 0 ]; then',
        *["  " + line for line in stop(i18n.t("privileged.timeout"), "timeout")],
        "fi",
        'case "$answer" in ' + _yes_pattern() + ') ;; *)',
        *["  " + line for line in stop(i18n.t("privileged.refused"), "refused")],
        "  ;;",
        "esac",
        "printf '%s\\n' " + shlex.quote(i18n.t("privileged.approved")),
        "( " + exec_line + " ) 2>&1 | tee " + shlex.quote(output_file),
        "code=${PIPESTATUS[0]}",
        write_code("$code").replace("'$code'", '"$code"'),
        "printf '\\n%s %s\\n' " + shlex.quote(i18n.t("privileged.exit")) + ' "$code"',
        "printf '%s' " + shlex.quote(i18n.t("privileged.close")),
        "read -r _",
        'exit "$code"',
        "",
    ]
    return "\n".join(lines)


def _read_text(path, limit=None):
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            return handle.read(limit)
    except OSError:
        return ""


def approved_run(argv, cwd=None, question="", command="", seconds=APPROVAL_SECONDS):
    """Asks the person on a separate tmux screen, then runs `argv` there if they say yes.

    Returns (status, code, output). `status` is `ran` (code and output are set), `refused`,
    `timeout`, `interrupted` (the screen closed before the command ended), `no_tmux` (no attached
    client: nothing ran) or `no_command`. The question and the full command are shown before the
    answer; `argv` is what runs (through pkexec, built by the caller) and is never run without it.
    """
    argv = [str(arg) for arg in argv]
    if not argv:
        return "no_command", None, ""
    attached = clients()
    if not attached:
        return "no_tmux", None, ""
    directory = tempfile.mkdtemp(prefix="ws-privileged-")
    code_file = os.path.join(directory, "exit")
    output_file = os.path.join(directory, "output")
    script = os.path.join(directory, "run.sh")
    try:
        # pkexec asks for the password its own way (the system agent); only the question is ours.
        with open(script, "w", encoding="utf-8") as handle:
            handle.write(_approved_script(command or shlex.join(argv), question, seconds,
                                          shlex.join(argv), code_file, output_file))
        os.chmod(script, 0o700)
        popup(quote_cmd([BASH, script]), str(cwd or os.getcwd()), client=attached[0])
        status = _read_text(code_file).strip()
        output = _read_text(output_file, MAX_OUTPUT)
        if status in ("refused", "timeout"):
            return status, None, ""
        try:
            return "ran", int(status), output
        except ValueError:
            return "interrupted", None, output
    finally:
        shutil.rmtree(directory, ignore_errors=True)

