"""The only place that runs the tmux command. Panels and helpers call these functions instead.

run() never raises: tmux missing, a timeout or a failing command gives "" so a panel keeps drawing
while the server is busy or gone. Pass timeout=None for commands that block until the user closes
them (display-popup -E).
"""
import os
import shlex
import subprocess

TIMEOUT = 3


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


def popup(cmd, cwd, size="92%"):
    """Opens `cmd` in a tmux popup at `size`x`size` in `cwd`, blocking until the user closes it."""
    run("display-popup", "-E", "-w", size, "-h", size, "-d", cwd, cmd, timeout=None)


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

