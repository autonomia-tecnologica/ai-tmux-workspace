#!/usr/bin/env python3
"""Context menu layer: the only module that declares menu entries per item type.

A panel asks for `actions(viewer, key) -> [(label, active, action)]` and the `viewer` draws the
overlay, navigates and runs the action; a panel never builds menu entries itself. Today only repo
items (key `("r", path)`) have a menu; another item type enters here, not in the panel.

The repo menu has exactly two actions, both writing the user config through `lib` (backup + textual
edit, comments preserved) and taking effect on the next redraw: no tmux state and no `restart`.

  "Mover para grupo"          picks a declared group or asks for a new name
  "Marcar como irmão de..."   picks another repo; the relation is symmetric, like `siblings`

`on_input`/`header` are the inline text prompt used by "new group"; the panel passes both to the
viewer, so the prompt lives in this layer too.
"""
import os
import time

import lib
from i18n import t
from viewer import DIM, R

NOTE_S = 4

_STATE = {"prompt": None, "note": "", "until": 0.0}


def actions(v, key):
    """Menu entries for the item under the cursor. Only repo items have a menu today."""
    if not key or key[0] != "r":
        return []
    return repo_actions(v, key)


def repo_actions(v, key):
    """The two repo actions. The main repo cannot move to a group (it stays outside them)."""
    path = lib.expand_path(key[1])
    main = lib.read_main()
    can_move = not main or lib.expand_path(main[1]) != path
    return [(t("repos.menu.move_group"), can_move, open_groups),
            (t("repos.menu.sibling"), True, open_siblings)]


def open_groups(v, key):
    """Submenu of the declared groups, plus the new-group prompt."""
    items = [(name, True, move_to(name)) for name in lib.group_names()]
    items.append((t("repos.menu.new_group"), True, ask_group))
    v.open_menu(items, key)


def move_to(group):
    def action(v, key):
        move(v, key, group)
    return action


def ask_group(v, key):
    _STATE["prompt"] = {"key": key, "label": t("repos.group.new_prompt"), "text": ""}
    v.redraw.set()


def open_siblings(v, key):
    """Submenu with every other listed repo; the current repo is not offered."""
    current = lib.expand_path(key[1])
    seen = {current}
    items = []
    main = lib.read_main()
    candidates = ([main] if main else []) + [repo for _group, repos in lib.read_repos() for repo in repos]
    for name, path in candidates:
        path = lib.expand_path(path)
        if path in seen:
            continue
        seen.add(path)
        items.append((name, True, sibling_to(path)))
    if not items:
        items.append((t("repos.menu.no_siblings"), False, None))
    v.open_menu(items, key)


def sibling_to(other):
    def action(v, key):
        sibling(v, key, other)
    return action


def move(v, key, group):
    """Writes the new group and reports through the header; the panel rereads the config."""
    try:
        lib.move_repo_to_group(key[1], group)
        notice(t("repos.moved", repo=os.path.basename(lib.expand_path(key[1])), group=group))
    except (OSError, ValueError) as error:
        notice(str(error))
    v.redraw.set()


def sibling(v, key, other):
    """Writes the symmetric relation and reports through the header; no restart is needed."""
    try:
        lib.mark_repo_sibling(key[1], other)
        notice(t("repos.sibling_added", repo=os.path.basename(lib.expand_path(key[1])),
                 other=os.path.basename(other)))
    except (OSError, ValueError) as error:
        notice(str(error))
    v.redraw.set()


def notice(text):
    _STATE["note"], _STATE["until"] = text, time.time() + NOTE_S


def on_input(v, k):
    """Inline editor for the new-group name; consumes every key while the prompt is active."""
    prompt = _STATE["prompt"]
    if not prompt:
        return False
    if k == "esc":
        _STATE["prompt"] = None
        v.redraw.set()
        return True
    if k.startswith("\x1b"):        # arrows and other escape sequences neither edit nor cancel
        return True
    if k in ("\r", "\n"):
        name, key = prompt["text"].strip(), prompt["key"]
        _STATE["prompt"] = None
        if name:
            move(v, key, name)
        else:
            v.redraw.set()
        return True
    if k in ("\x7f", "\x08"):       # Backspace
        prompt["text"] = prompt["text"][:-1]
        v.redraw.set()
        return True
    if k.isprintable():
        prompt["text"] += k
        v.redraw.set()
    return True


def header():
    """Header rows this layer owns: the active prompt and the last notice (for the panel to add)."""
    rows = []
    prompt = _STATE["prompt"]
    if prompt:
        rows.append(f" {DIM}{prompt['label']}{prompt['text']}▏{R}")
    if _STATE["note"] and time.time() < _STATE["until"]:
        rows.append(f" {_STATE['note']}")
    elif _STATE["note"]:
        _STATE["note"] = ""
    return rows
