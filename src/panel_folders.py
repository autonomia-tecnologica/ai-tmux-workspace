#!/usr/bin/env python3
"""Free folders: the navigation tab of the home window (item 82).

The folders come from "free_folders" in config.yaml (Documents, Pictures, Videos...). They are not
repos: this panel opens no window and starts no agent session. Enter opens or closes a folder and
opens a file through the one door, `opener.open_file` (the configured viewer from item 44, then the
OS default program). A type neither covers does not open; the header says so.

Keys: ("d", path) for a folder, ("f", path) for a file. The root rows carry the configured label.
Expansion is memory-only, so a panel restart (or a mode switch) starts with the roots closed.
"""
import os
import time

import glyphs
import icons
import lib
import opener
from i18n import t
from tree_state import Expander
from viewer import BLUE, B, GRAY, DIM, R, Row, Viewer

OPEN = Expander()          # expanded folders of this run (memory only)
LIMIT = 400                # entries read from one folder: a browser, not a full index
NOTE_S = 4                 # seconds the header note stays
VIEW = {"note": "", "note_until": 0.0}
USE_ICONS = icons.enabled()


def note(text):
    VIEW["note"], VIEW["note_until"] = text, time.time() + NOTE_S


def list_dir(folder):
    """Entries of `folder`, folders first then by name, without dot files. [] when it cannot be read."""
    try:
        items = [e for e in os.scandir(folder) if not e.name.startswith(".")]
    except OSError:
        return []
    items.sort(key=lambda e: (not e.is_dir(follow_symlinks=False), e.name.lower()))
    return items[:LIMIT]


def collect(v):
    header = [Row(f" {DIM}{t('folders.hint')}{R}")]
    if time.time() < VIEW["note_until"]:
        header.append(Row(f" {VIEW['note']}"))
    body = []
    roots = lib.free_folders()
    if not roots:
        body.append(Row(f" {GRAY}{t('folders.none')}{R}"))
        return header, body

    def descend(folder, level, label=None):
        opened = OPEN.is_open(folder)
        indent = "  " * level
        arrow = glyphs.arrow(opened)
        if not os.path.isdir(folder):
            body.append(Row(f" {indent}{GRAY}{arrow} {label or os.path.basename(folder)}"
                            f" · {t('folders.missing')}{R}", ("d", folder)))
            return
        icon = glyphs.dir_icon(opened) if USE_ICONS else ""
        name = f"{B}{label}{R}" if label else f"{BLUE}{os.path.basename(folder)}/{R}"
        body.append(Row(f" {indent}{GRAY}{arrow}{R} {icon}{name}", ("d", folder)))
        if not opened:
            return
        for entry in list_dir(folder):
            if entry.is_dir(follow_symlinks=False):
                descend(entry.path, level + 1)
            else:
                icon = glyphs.file_icon(entry.name) if USE_ICONS else ""
                body.append(Row(f" {indent}    {icon}{entry.name}", ("f", entry.path)))

    for label, path in roots:
        descend(path, 0, label)
    return header, body


def footer(v):
    """Absolute path of the selected entry in bold (the row itself is cut at the pane width)."""
    if not v.key:
        return ""
    return f"{B}{os.path.abspath(v.key[1])}{'/' if v.key[0] == 'd' else ''}{R}"


def enter(v, key, _pressed):
    kind, path = key
    if kind == "d":
        if os.path.isdir(path):
            OPEN.toggle(path)
        else:
            note(t("folders.missing_note", path=path))
        v.redraw.set()
        return
    if not opener.open_file(path):
        note(t("folders.open_failed", name=os.path.basename(path)))
    v.redraw.set()


def parent_of(path):
    """Parent folder inside the same free folder, or None. A root is the top: nothing above it."""
    for _, root in lib.free_folders():
        base = root if root == os.sep else root.rstrip(os.sep) + os.sep
        if path != root and path.startswith(base):
            parent = os.path.dirname(path)
            return parent if parent != root else None
    return None


def on_key(v, k):
    """Left/Right fold the folder under the cursor; on the left of an open folder, climb to its parent."""
    if k not in ("left", "right") or not v.key:
        return
    kind, path = v.key
    new = OPEN.handle_arrow(k, path, parent=parent_of, folder=kind == "d")
    if new and new != path:
        v.key = ("d", new)
    v.redraw.set()


if __name__ == "__main__":
    roots = lib.free_folders()
    Viewer(collect, interval=2.0, cursor=True, on_enter=enter, on_key=on_key, footer=footer,
           initial_key=("d", roots[0][1]) if roots else None, max_footer_lines=None).run()
