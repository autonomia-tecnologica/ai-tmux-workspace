#!/usr/bin/env python3
"""Top bar of each panel (1 row; arg: session|terminal|repos|tasks|side):
[SESSÃO] [SESSÃO (2)] [SESSÃO (3)] ... [+]. In `repos` it is REPOS and SESSIONS (plus FOLDERS in the
home window) and the listed plugins (no +); in `tasks` it is only the panel title (one tab, no +).
In `side` mode the tabs are fixed (FILES, SESSIONS, DIFF, GITHUB) and choose what the right panel shows.

Clicking a tab switches, clicking + opens a new session. With the panel focused, ←/→ move the
cursor (blue) between the tabs and the +, and Enter activates; the active tab turns gray while the
cursor is elsewhere. Losing the focus (tmux focus events) sends the cursor back to the active tab,
blue again, and activates nothing. Draws once and waits;
on SIGUSR1 (`workspace` signals a tab change, without respawning the panel) or SIGWINCH (a real
resize) it rereads the state and redraws only what changed. A quiet 60s timeout does the same, so a
lost signal only delays the redraw.

Terminal-ownership exception: it paints the strip itself (full clear + cbreak) instead of returning
`collect(viewer)` rows. It is registered, with this reason, in tests/test_architecture.py until that
debt is paid.
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
from colors import R, TAB_ACTIVE, TAB_IDLE
from viewer import direction

SGR = re.compile(rb"\x1b\[<(\d+);(\d+);(\d+)([Mm])")
STYLE = {"active": TAB_ACTIVE, "idle": TAB_IDLE}


_KIND = sys.argv[1] if len(sys.argv) > 1 and sys.argv[1] in ("terminal", "side", "repos", "tasks", "session", "claude") else "claude"
KIND = "session" if _KIND in ("claude", "session") else _KIND
NO_PLUS = ("side", "repos", "tasks")
PAD = 1 if KIND in ("session", "repos", "side") else 0   # top-row bars get a blank line above, off the status bar


def t(key):
    return i18n.t(f"tabs.{key}")


def kind_label(kind):
    """Label of a tab kind: the generic session label for the session strip."""
    return t(kind)


SIDE_TABS = [(k, t(k)) for k in ("tree", "diff", "github")]
VARS = {"session": ("@ws_tabs", "@ws_active"), "terminal": ("@ws_ttabs", "@ws_term_active")}


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


def is_home(win):
    """True when `win` is the home window: the window of the main repo from config.yaml.

    Free folders are a navigation tab of the home window only (item 82); every other repo window
    keeps just REPOS and SESSIONS."""
    main = lib.read_main()
    return bool(main) and tmux.show_w(win, "@ws_repo") == main[1]


def left_tabs(home):
    """Tabs of the left strip: REPOS and SESSIONS, plus FOLDERS in the home window."""
    tabs = [("repos", t("repos")), ("sessions", t("sessions"))]
    if home:
        tabs.append(("folders", t("folders")))
    return tabs


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


def session_labels(ids, profile_of, alive):
    """[(pane_id, label)] of the alive session panes, numbering repeated profiles.

    `profile_of` returns a pane's profile name ("" when unknown); `alive` reports whether the pane
    still exists. The label is the upper-cased profile (the generic SESSÃO when empty); the 2nd, 3rd
    occurrence of a profile gets " (2)", " (3)" in the order they appear.
    """
    seen, tabs = {}, []
    for p in ids:
        if not alive(p):
            continue
        profile = (profile_of(p) or "").upper() or t("session")
        seen[profile] = seen.get(profile, 0) + 1
        label = profile if seen[profile] == 1 else f"{profile} ({seen[profile]})"
        tabs.append((p, label))
    return tabs


def state():
    win =tmux.run("display", "-p", "-t", os.environ.get("TMUX_PANE", ""), "#{window_id}")
    if KIND == "side":
        mode = tmux.run("show", "-wqv", "-t", win, "@ws_side")
        return win, SIDE_TABS, mode if mode in ("diff", "github") else "tree"
    if KIND == "repos":
        # REPOS, SESSIONS (and FOLDERS in the home window) plus the listed plugins; the active one is
        # the window the panel runs in
        name = tmux.run("display", "-p", "-t", win, "#{window_name}")
        mode = tmux.run("show", "-wqv", "-t", win, "@ws_left")
        home = is_home(win)
        if mode == "folders" and not home:
            mode = "repos"
        tabs = left_tabs(home) + plugin_tabs(session_name())
        active = (name if name.startswith("plugin:")
                  else mode if mode in ("sessions", "folders") else "repos")
        return win, tabs, active
    if KIND == "tasks":
        return win, [(KIND, t(KIND))], KIND
    if KIND == "session":
        ids = tmux.run("show", "-wqv", "-t", win, "@ws_tabs").split()
        active = tmux.run("show", "-wqv", "-t", win, "@ws_active")
        tabs = session_labels(
            ids,
            profile_of=lambda p: tmux.display("#{@ws_tab_profile}", target=p),
            alive=lambda p: bool(tmux.display("#{?pane_id,#{@role},}", target=p)),
        )
        return win, tabs, active
    v_tabs, v_active = VARS[KIND]
    ids = tmux.run("show", "-wqv", "-t", win, v_tabs).split()
    active = tmux.run("show", "-wqv", "-t", win, v_active)
    tabs = []
    for p in ids:
        if tmux.run("display", "-p", "-t", p, "#{?pane_id,#{@role},}"):
            # the main tab has no number; the others are numbered by creation order (2, 3, ...)
            tabs.append((p, kind_label(KIND) if not tabs else f"{kind_label(KIND)} ({len(tabs) + 1})"))
    return win, tabs, active


def item_styles(items, active, cursor, focused):
    """Style of each item of the bar, {item: "active" | "idle"}, from the state alone (no tmux).

    With the panel focused, the cursor's item is "active" (blue) and the active tab is "idle" (gray)
    when the cursor is elsewhere; the + follows the same rule. Without focus only the active tab is
    "active": the cursor is back on it (see `set_focus`), so nothing else is highlighted.
    """
    return {item: "active" if (item == cursor if focused else item == active) else "idle"
            for item in items}


def draw(tabs, active, cursor=None, focused=False):
    """Return (text, [(start, end, id)]) with 0-based columns."""
    out, map, x = "", [], 0
    items = tabs + ([] if KIND in NO_PLUS else [("new", "+")])
    styles = item_styles([p for p, _ in items], active, cursor, focused)
    for p, name in items:
        label = f" {name} "
        out += f"{STYLE[styles[p]]}{label}{R} "
        map.append((x, x + len(label) - 1, p))
        x += len(label) + 1
    return out.rstrip(" "), map   # no trailing space: on a full-width bar it would wrap and scroll the bar up


def view_items(tabs):
    """Keys of the bar, in order: the tabs plus the + when the kind has one."""
    return [p for p, _ in tabs] + ([] if KIND in NO_PLUS else ["new"])


def cursor_index(items, active, previous=None):
    """Keyboard cursor position: keep `previous` when it still exists, else the active tab."""
    if previous in items:
        return items.index(previous)
    return items.index(active) if active in items else 0


def set_focus(view, focused):
    """Records the panel's focus. The cursor only lives while the panel is focused, so either way it
    goes back to the active tab; nothing is activated."""
    view["focused"] = focused
    view["cursor"] = view["items"][cursor_index(view["items"], view["active"])]


def load(view):
    """Rereads the strip into `view` (win, tabs, active, items, cursor), clamping the cursor.

    Signals arrive after a tab opened, closed or switched, so the captured state must be replaced by
    a fresh `state()`; the cursor stays on the tab the person had focused while it still exists.
    After an activation (`follow`, set by `open`) the cursor moves to the new active tab instead.
    """
    win, tabs, active = state()
    items = view_items(tabs)
    previous = None if view.pop("follow", False) else view.get("cursor")
    cursor = items[cursor_index(items, active, previous)]
    view.update(win=win, tabs=tabs, active=active, items=items, cursor=cursor)
    return view


def paint(view, first=False):
    """Draws the bar; returns False and writes nothing when the state did not change."""
    text, mapping = draw(view["tabs"], view["active"], view["cursor"], view.get("focused", False))
    if not first and text == view.get("text"):
        return False
    view["text"], view["map"] = text, mapping
    out = "\x1b[?25l\x1b[2J" if first else "\x1b[2J"
    out += f"\x1b[{PAD + 1};1H" + text
    if first:
        out += "\x1b[?1000h\x1b[?1006h\x1b[?1004h"   # mouse, and focus in/out events (\x1b[I, \x1b[O)
    sys.stdout.write(out)
    sys.stdout.flush()
    return True


def main():
    view = {}
    load(view)
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    tty.setcbreak(fd)
    paint(view, first=True)

    def open(pane):
        win = view["win"]
        view["follow"] = True   # an activation (Enter or click): the cursor follows the new active tab
        if KIND == "repos" and pane.startswith("plugin:"):
            wid = plugin_window(pane)
            if wid:
                tmux.run("select-window", "-t", wid)
            return
        if KIND == "repos" and pane in ("repos", "sessions") and tmux.run("display", "-p", "-t", win, "#{window_name}").startswith("plugin:"):
            wid = repo_window()
            if wid:
                tmux.run("select-window", "-t", wid)
                subprocess.Popen([lib.WS, "left", pane, wid], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return
        if KIND == "repos" and pane in ("repos", "sessions", "folders"):
            subprocess.Popen([lib.WS, "left", pane, win], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return
        if KIND in ("repos", "tasks"):
            return
        if KIND == "side":
            cmd = [lib.WS, "side", pane, win]
        elif KIND == "session":
            cmd = [lib.WS, "session-menu", win] if pane == "new" else [lib.WS, "tab-show", win, pane]
        else:
            cmd = [lib.WS, "tab-new", win, KIND] if pane == "new" else [lib.WS, "tab-show", win, pane]
        subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def move(step):
        items = view["items"]
        cur = items.index(view["cursor"])
        view["cursor"] = items[max(0, min(len(items) - 1, cur + step))]
        paint(view)

    # SIGUSR1 comes from `workspace` when a tab changes (the strip is not respawned any more); SIGWINCH
    # is a real resize. The wakeup fd ends the select; the loop then rereads the state and repaints only
    # when something changed.
    wake_r, wake_w = os.pipe()
    os.set_blocking(wake_w, False)
    os.set_blocking(wake_r, False)
    signal.set_wakeup_fd(wake_w)
    signal.signal(signal.SIGUSR1, lambda *_: None)
    signal.signal(signal.SIGWINCH, lambda *_: None)

    def consume_wake():
        try:
            while os.read(wake_r, 64):
                pass
        except BlockingIOError:
            pass

    try:
        buf = b""
        while True:
            try:
                ready = select.select([fd, wake_r], [], [], 60)[0]
            except InterruptedError:
                continue
            if wake_r in ready:
                consume_wake()
                load(view)
                paint(view)
                if fd not in ready:
                    continue
            if fd not in ready:   # quiet 60s timeout: reread anyway (paint writes only on change)
                load(view)
                paint(view)
                continue
            try:
                buf += os.read(fd, 4096)
            except InterruptedError:
                continue
            for m in SGR.finditer(buf):
                b, x, press = int(m.group(1)), int(m.group(2)) - 1, m.group(4) == b"M"
                if b == 0 and press:
                    for offset, end, pane in view["map"]:
                        if offset <= x <= end:
                            open(pane)
                            break
            buf = SGR.sub(b"", buf)
            # \x1b[I / \x1b[O are focus in/out; \x1b[O is focus out unless an SS3 arrow (\x1bO C/D) follows
            for k in re.findall(rb"\x1b\[I|\x1b\[O(?![CD])|\x1b[\[O][CD]|\r|\n|[hl]", buf):
                if k == b"\x1b[I":
                    set_focus(view, True)
                    paint(view)
                    continue
                if k == b"\x1b[O":
                    set_focus(view, False)
                    paint(view)
                    continue
                view["focused"] = True   # a key only reaches a focused panel
                d = direction(k.decode("latin1"))
                if d == "left":
                    move(-1)
                elif d == "right":
                    move(1)
                else:
                    open(view["cursor"])
            buf = b"" if len(buf) < 64 else buf[-8:]
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)
        sys.stdout.write("\x1b[?1004l\x1b[?1006l\x1b[?1000l\x1b[?25h")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
