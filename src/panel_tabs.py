#!/usr/bin/env python3
"""Top bar of each panel (1 row; arg: claude|terminal|repos|tasks|side):
[CLAUDE] [CLAUDE (2)] [CLAUDE (3)] ... [+]. In `repos` it is REPOS and the listed plugins (no +); in `tasks` it is only the panel title (one tab, no +).
In `side` mode the tabs are fixed (FILES, SESSIONS, DIFF, GITHUB) and choose what the right panel shows.

Clicking a tab switches, clicking + opens a temporary session. With the panel focused, ←/→ move the
cursor (lighter background) between the tabs and the +, and Enter activates. Draws and waits on mouse/keyboard;
`workspace` restarts this panel (respawn-pane) on every tab change.
"""
import os
import re
import select
import signal
import subprocess
import sys
import termios
import tty

import i18n
import lib
import tmux
from colors import R, TAB_ACTIVE, TAB_CURSOR, TAB_IDLE, TAB_NEW
from viewer import direction

SGR = re.compile(rb"\x1b\[<(\d+);(\d+);(\d+)([Mm])")


KIND = sys.argv[1] if len(sys.argv) > 1 and sys.argv[1] in ("terminal", "side", "repos", "tasks") else "claude"
NO_PLUS = ("side", "repos", "tasks")
PAD = 1 if KIND in ("claude", "repos", "side") else 0   # top-row bars get a blank line above, off the status bar   # top-row bars get a blank line above, off the status bar


def t(key):
    return i18n.t(f"tabs.{key}")


SIDE_TABS = [(k, t(k)) for k in ("tree", "diff", "github")]
VARS = {"claude": ("@ws_tabs", "@ws_active"), "terminal": ("@ws_ttabs", "@ws_term_active")}


def session_name():
    return tmux.run("display", "-p", "-t", os.environ.get("TMUX_PANE", ""), "#{session_name}")


def plugin_tabs(profile):
    """[(\"plugin:<id>\", label)] for each valid plugin listed in config.yaml (broken ones have no window)."""
    tabs = []
    for entry in lib.listed_plugins(profile):
        try:
            id_, _, label, _ = lib.load_plugin(entry)
        except lib.PluginError:
            continue
        tabs.append((f"plugin:{id_}", label))
    return tabs


def plugin_window(key):
    """Window id of the plugin window named like the tab key (plugin windows are named plugin:<id>)."""
    for line in tmux.run("list-windows", "-t", session_name(), "-F", "#{window_id}|#{window_name}").splitlines():
        wid, _, name = line.partition("|")
        if name == key:
            return wid
    return ""


def repo_window():
    """Window to go back to from a plugin window: the previous window (tmux's own history) when it is a
    repo, else the first repo window of the session. Empty when the session has no repo window."""
    session = session_name()
    previous = tmux.run("display", "-p", "-t", f"{session}:{{last}}", "#{window_id}")
    if previous and tmux.run("show", "-wqv", "-t", previous, "@ws_repo"):
        return previous
    for line in tmux.run("list-windows", "-t", session, "-F", "#{window_id}|#{@ws_repo}").splitlines():
        wid, _, repo = line.partition("|")
        if repo:
            return wid
    return ""


def state():
    win =tmux.run("display", "-p", "-t", os.environ.get("TMUX_PANE", ""), "#{window_id}")
    if KIND == "side":
        mode = tmux.run("show", "-wqv", "-t", win, "@ws_side")
        return win, SIDE_TABS, mode if mode in ("diff", "github") else "tree"
    if KIND == "repos":
        # REPOS plus the listed plugins; the active one is the window the panel runs in
        name = tmux.run("display", "-p", "-t", win, "#{window_name}")
        mode = tmux.run("show", "-wqv", "-t", win, "@ws_left")
        active = name if name.startswith("plugin:") else ("sessions" if mode == "sessions" else "repos")
        return win, [("repos", t("repos")), ("sessions", t("sessions"))] + plugin_tabs(session_name()), active
    if KIND == "tasks":
        return win, [(KIND, t(KIND))], KIND
    v_tabs, v_active = VARS[KIND]
    ids = tmux.run("show", "-wqv", "-t", win, v_tabs).split()
    active = tmux.run("show", "-wqv", "-t", win, v_active)
    tabs = []
    for p in ids:
        if tmux.run("display", "-p", "-t", p, "#{?pane_id,#{@role},}"):
            # the main tab has no number; the others are numbered by creation order (2, 3, ...)
            tabs.append((p, t(KIND) if not tabs else f"{t(KIND)} ({len(tabs) + 1})"))
    return win, tabs, active


def draw(tabs, active, cursor=None):
    """Return (text, [(start, end, id)]) with 0-based columns."""
    out, map, x = "", [], 0
    for p, name in tabs + ([] if KIND in NO_PLUS else [("new", "+")]):
        label = f" {name} "
        if p == active:
            color = TAB_ACTIVE
        elif p == "new":
            color = TAB_NEW
        else:
            color = TAB_IDLE
        if p == cursor and p != active:   # keyboard cursor: lighter background (no underline)
            color = TAB_CURSOR
        out += f"{color}{label}{R} "
        map.append((x, x + len(label) - 1, p))
        x += len(label) + 1
    return out.rstrip(" "), map   # no trailing space: on a full-width bar it would wrap and scroll the bar up


def main():
    win, tabs, active = state()
    items = [p for p, _ in tabs] + ([] if KIND in NO_PLUS else ["new"])
    cur = items.index(active) if active in items else 0
    text, map = draw(tabs, active, items[cur])
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    tty.setcbreak(fd)
    sys.stdout.write("\x1b[?25l\x1b[2J" + f"\x1b[{PAD + 1};1H" + text + "\x1b[?1000h\x1b[?1006h")
    sys.stdout.flush()

    def open(p):
        if KIND == "repos" and p.startswith("plugin:"):
            wid = plugin_window(p)
            if wid:
                tmux.run("select-window", "-t", wid)
            return
        if KIND == "repos" and p in ("repos", "sessions") and tmux.run("display", "-p", "-t", win, "#{window_name}").startswith("plugin:"):
            wid = repo_window()
            if wid:
                tmux.run("select-window", "-t", wid)
                subprocess.Popen([lib.WS, "left", p, wid], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return
        if KIND == "repos" and p in ("repos", "sessions"):
            subprocess.Popen([lib.WS, "left", p, win], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return
        if KIND in ("repos", "tasks"):
            return
        if KIND == "side":
            cmd = [lib.WS, "side", p, win]
        else:
            cmd = [lib.WS, "tab-new", win, KIND] if p == "new" else [lib.WS, "tab-show", win, p]
        subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    cursor = [cur]

    def redraw(*_):   # resize (fit_window) can scroll the bar up: clear and draw again on its row
        sys.stdout.write("\x1b[2J" + f"\x1b[{PAD + 1};1H" + draw(tabs, active, items[cursor[0]])[0])
        sys.stdout.flush()

    signal.signal(signal.SIGWINCH, redraw)
    try:
        buf = b""
        while True:
            try:
                if not select.select([fd], [], [], 60)[0]:
                    continue
                buf += os.read(fd, 4096)
            except InterruptedError:
                continue
            for m in SGR.finditer(buf):
                b, x, press = int(m.group(1)), int(m.group(2)) - 1, m.group(4) == b"M"
                if b == 0 and press:
                    for offset, end, p in map:
                        if offset <= x <= end:
                            open(p)
                            break
            buf = SGR.sub(b"", buf)
            step = 0
            for k in re.findall(rb"\x1b[\[O][CD]|\r|\n|[hl]", buf):
                d = direction(k.decode("latin1"))
                if d == "left":
                    step = -1
                elif d == "right":
                    step = 1
                else:
                    open(items[cur])
                    continue
                cur = max(0, min(len(items) - 1, cur + step))
                cursor[0] = cur
                sys.stdout.write(f"\x1b[{PAD + 1};1H" + draw(tabs, active, items[cur])[0])
                sys.stdout.flush()
            buf = b"" if len(buf) < 64 else buf[-8:]
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)
        sys.stdout.write("\x1b[?1006l\x1b[?1000l\x1b[?25h")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
