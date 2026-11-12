#!/usr/bin/env python3
"""File tree of the window repo. Enter opens/closes a folder or opens the file in the editor."""
import os
import shlex
import subprocess

import clipboard
import glyphs
import icons
import lib
from i18n import t
import opener
import tmux
from tree_state import Expander
from viewer import YELLOW, BLUE, B, GRAY, DIM, R, GREEN, Row, Viewer

REPO = lib.current_repo()
IGNORED = {".git", "node_modules", "__pycache__", ".venv", "target", ".mypy_cache", ".pytest_cache"}
OPEN_FOLDERS = Expander()   # folders expanded by the user (closed by default)
OPEN_FOLDERS.open(REPO)
LIMIT = 400
USE_ICONS = icons.enabled()


def status_git():
    st = lib.git_status(REPO) or {}
    return {os.path.join(REPO, p): xy for p, xy in st.items()}


def ignored_names(folder, items):
    """Names among `items` that git ignores (one `git check-ignore` call per folder)."""
    if not items:
        return set()
    rc, out = lib.git(REPO, "check-ignore", "-z", "--stdin",
                      input="\0".join(e.path for e in items) + "\0", timeout=5)
    if rc != 0:   # 1 = nothing ignored (empty output); an error also lands here
        return set()
    return {os.path.basename(x.rstrip("/")) for x in out.split("\0") if x}


def list_dir(folder):
    try:
        items = [e for e in os.scandir(folder) if e.name not in IGNORED]
    except OSError:
        return []
    items.sort(key=lambda e: (not e.is_dir(follow_symlinks=False), e.name.lower()))
    return items[:LIMIT]


_last_cwd = [None]


def follow_terminal(v):
    """When the shell in the active terminal tab changes directory, expand that folder (only it,
    not its children) and move the selection there. Only reacts to a change, so manual navigation
    in the tree is not overridden."""
    pane = os.environ.get("TMUX_PANE")
    if not pane:
        return
    win = tmux.display("#{window_id}", target=pane)
    target = tmux.show_w(win, "@ws_term_active")
    if not target:
        target = tmux.run("list-panes", "-t", win, "-F", "#{pane_id}|#{@role}")
        target = next((l.split("|")[0] for l in target.splitlines() if l.endswith("|terminal")), "")
    if not target:
        return
    cwd = os.path.realpath(tmux.display("#{pane_current_path}", target=target) or REPO)
    if cwd == _last_cwd[0]:
        return
    _last_cwd[0] = cwd
    repo = os.path.realpath(REPO)
    if cwd == repo:                 # the root is not a row: select the first entry and scroll to the top
        first = next(iter(list_dir(REPO)), None)
        if first:
            v.key = ("d" if first.is_dir(follow_symlinks=False) else "f", first.path)
            v.top = 0
            v._wake()
        return
    if not cwd.startswith(repo + "/"):
        return
    rel = os.path.relpath(cwd, repo).split(os.sep)
    for i in range(len(rel)):       # open the ancestors and the folder itself (its subfolders stay closed)
        OPEN_FOLDERS.open(os.path.join(REPO, *rel[:i + 1]))
    v.key = ("d", os.path.join(REPO, *rel))
    v._wake()


def collect(v):
    follow_terminal(v)
    st = status_git()
    dirty = {os.path.dirname(p) for p in st}
    for p in list(dirty):
        while p.startswith(REPO) and p != REPO:
            p = os.path.dirname(p)
            dirty.add(p)
    header = []
    body = []

    def descend(folder, level, ignored=False):
        items = list_dir(folder)
        ign = {e.name for e in items} if ignored else ignored_names(folder, items)
        for e in items:
            ind = "  " * level
            if e.is_dir(follow_symlinks=False):
                opened = OPEN_FOLDERS.is_open(e.path)
                mark = f" {glyphs.dirty_mark()}" if e.path in dirty else ""
                ic = glyphs.dir_icon(opened, e.name in ign) if USE_ICONS else ""
                name = f"{DIM}{e.name}/{R}" if e.name in ign else f"{BLUE}{e.name}/{R}"
                body.append(Row(f" {ind}{GRAY}{glyphs.arrow(opened)}{R} {ic}{name}{mark}",
                                   ("d", e.path)))
                if opened:
                    descend(e.path, level + 1, e.name in ign)
            else:
                s = st.get(e.path)
                color = DIM if e.name in ign else "" if not s else (GREEN if s.strip() in ("??", "A") else YELLOW)
                ic = glyphs.file_icon(e.name, e.name in ign) if USE_ICONS else ""
                body.append(Row(f" {ind}  {ic}{color}{e.name}{R}", ("f", e.path)))

    descend(REPO, 0)
    return header, body


RENAME = {"active": False, "key": None, "name": ""}


def footer(v):
    """Absolute path of the selected entry in bold (the row itself is cut at the pane width). While
    renaming, shows the prompt and the inline buffer with a block cursor."""
    if RENAME["active"]:
        return f"{B}{t('tree.rename_prompt')}{R} {RENAME['name']}█"
    if not v.key or v.key[1] == REPO:
        return ""
    path = os.path.abspath(v.key[1])
    return f"{B}{path}{'/' if v.key[0] == 'd' else ''}{R}"


def _popup_command(choice, path):
    """tmux command string that opens `path` in `choice` (fullscreen gives a 100%x100% popup)."""
    size = "100%" if choice.get("fullscreen") else "92%"
    line = f"{choice['command']} {shlex.quote(path)}"
    command = (f"display-popup -E -w {size} -h {size} -d {shlex.quote(REPO)} "
               f"{shlex.quote(line)}")
    return command.replace("#", "##")     # tmux expands formats in the command string


def open_with_editor(path):
    """Editor menu for the file type: the configured entries that exist, or the fallback chain."""
    choices = opener.choices(path)
    if not choices:
        tmux.message(t("editor.none"))
        return
    items = [(e["name"], str(i + 1), _popup_command(e, path))
             for i, e in enumerate(choices)]
    tmux.menu(t("editor.menu_title", name=os.path.basename(path)), items)


def enter(v, key, _pressed):
    kind, path = key
    if kind == "d":
        OPEN_FOLDERS.toggle(path)
        v.redraw.set()
    elif opener.open_file(path):
        return
    else:
        open_with_editor(path)


def _state(v):
    return getattr(v, "redraw", None)


def _rename_error(name):
    """i18n message for an invalid new name, or "" when it is acceptable."""
    if not name:
        return t("tree.rename_empty")
    if os.path.isabs(name) or "/" in name or "\\" in name or name in (".", ".."):
        return t("tree.rename_invalid")
    return ""


def rename_to(v, key, name):
    """Renames the file in `key` to `name` (same folder). Returns True on success and moves the
    cursor to the new path; on any failure shows an i18n message and keeps the old path."""
    kind, current = key
    if kind != "f":
        return False
    error = _rename_error(name)
    if error:
        tmux.message(error)
        return False
    new = os.path.join(os.path.dirname(current), name)
    if os.path.normpath(new) == os.path.normpath(current):   # Enter without editing: nothing to do
        return True
    if os.path.lexists(new):
        tmux.message(t("tree.rename_exists", name=name))
        return False
    rel_old, rel_new = os.path.relpath(current, REPO), os.path.relpath(new, REPO)
    if lib.git(REPO, "ls-files", "--error-unmatch", rel_old)[0] == 0:
        ok = lib.git(REPO, "mv", "--", rel_old, rel_new)[0] == 0
    else:
        try:
            os.rename(current, new)
            ok = True
        except OSError:
            ok = False
    if ok:
        v.key = ("f", new)
        event = _state(v)
        if event:
            event.set()
        tmux.message(t("tree.renamed", name=name))
    else:
        tmux.message(t("tree.rename_failed", name=name))
    return ok


def rename(v, key):
    """Starts the inline text input with the current basename: the main Viewer's `on_input` callback
    consumes the keys until Enter (commit) or Esc (cancel)."""
    if key[0] != "f":
        return
    RENAME.update(active=True, key=key, name=os.path.basename(key[1]))
    v.redraw.set()


def on_input(v, k):
    """Inline rename editor. Returns True while it consumes the key, so navigation is blocked."""
    if not RENAME["active"]:
        return False
    if k == "esc":
        RENAME.update(active=False, key=None, name="")
        v.redraw.set()
        return True
    if k.startswith("\x1b"):        # arrows and other escape sequences do not edit or cancel
        return True
    if k in ("\r", "\n"):
        key, name = RENAME["key"], RENAME["name"]
        RENAME.update(active=False, key=None, name="")
        rename_to(v, key, name)
        v.redraw.set()
        return True
    if k in ("\x7f", "\x08"):       # Backspace
        RENAME["name"] = RENAME["name"][:-1]
        v.redraw.set()
        return True
    if k.isprintable():
        RENAME["name"] += k
        v.redraw.set()
    return True


def _copied(path):
    try:
        return clipboard.copy_file(path)
    except Exception:
        return None


def copy(v, key):
    """Puts the file on the clipboard as a file, to paste elsewhere. Only files reach here (see actions)."""
    if _copied(key[1]):
        msg = t("tree.copied", name=os.path.basename(key[1]))
    else:
        msg = t("tree.copy_failed")
    tmux.message(msg)


def actions(v, key):
    """Context menu (Space or right click). On a folder the actions are shown but unavailable."""
    file = key[0] == "f"
    return [(t("tree.menu_copy"), file, copy),
            (t("tree.menu_rename"), file, rename)]


def parent_of(path):
    """Parent folder inside the repo, or None (the repo root is not a row)."""
    parent = os.path.dirname(path)
    return parent if parent != REPO and parent.startswith(REPO + "/") else None


def on_key(v, k):
    """`e` opens the editor menu for the file under the cursor (also for HTML and media, which Enter
    sends to their viewer). Right opens the folder under the cursor, Left closes it (or jumps to the
    parent folder)."""
    if k == "e" and v.key and v.key[0] == "f":
        open_with_editor(v.key[1])
        return
    if k not in ("left", "right") or not v.key:
        return
    kind, path = v.key
    new = OPEN_FOLDERS.handle_arrow(k, path, parent=parent_of, folder=kind == "d")
    if new != path:
        v.key = ("d", new)
    v.redraw.set()


if __name__ == "__main__":
    Viewer(collect, interval=2.0, cursor=True, on_enter=enter, on_key=on_key, initial_key=("d", REPO), footer=footer,
           actions=actions, on_input=on_input, max_footer_lines=None).run()
