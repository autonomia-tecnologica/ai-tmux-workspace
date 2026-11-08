"""The only place that runs the tmux command. Panels and helpers call these functions instead.

run() never raises: tmux missing, a timeout or a failing command gives "" so a panel keeps drawing
while the server is busy or gone. Pass timeout=None for commands that block until the user closes
them (display-popup -E).
"""
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
