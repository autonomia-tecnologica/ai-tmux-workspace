"""The only place that runs the tmux command. Panels and helpers call these functions instead.

run() never raises: tmux missing, a timeout or a failing command gives "" so a panel keeps drawing
while the server is busy or gone. Pass timeout=None for commands that block until the user closes
them (display-popup -E).
"""
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


def live_panes():
    """Set of pane ids alive now, or None when tmux cannot be queried (different from "no panes")."""
    try:
        r = subprocess.run(["tmux", "list-panes", "-a", "-F", "#{pane_id}"], capture_output=True,
                           text=True, timeout=TIMEOUT, stdin=subprocess.DEVNULL)
    except Exception:
        return None
    return set(r.stdout.split()) if r.returncode == 0 else None


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


def popup(cmd, cwd):
    """Opens `cmd` in a tmux popup at 92%x92% in `cwd`, blocking until the user closes it."""
    run("display-popup", "-E", "-w", "92%", "-h", "92%", "-d", cwd, cmd, timeout=None)


def quote_cmd(argv):
    """Quotes a command for a tmux command string: shell-quotes each argument, then wraps the whole
    line in double quotes, escaping \\ " $ and doubling # (tmux expands formats in it)."""
    line = " ".join(shlex.quote(str(a)) for a in argv)
    escaped = line.replace("\\", "\\\\").replace('"', '\\"').replace("$", "\\$").replace("#", "##")
    return '"' + escaped + '"'


def confirm_then(prompt, argv):
    """Asks `prompt` in the tmux status line (y/n) and, on yes, runs argv in the background."""
    run("confirm-before", "-p", prompt.replace("#", "##"), f"run-shell -b {quote_cmd(argv)}", timeout=20)

