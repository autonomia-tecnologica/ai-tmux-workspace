#!/usr/bin/env python3
"""Repos panel: config.yaml groups, each repo with the agent state dot.

  ● red      blocked: the agent waits for an approval or an answer
  ● green    working: the agent (or a subagent) is running
  ● blue     done: the agent ended its turn
  ○ empty    idle: nothing to do
  ◌ dim      unknown: the agent process is gone

A repo shows the worst state of its tabs (blocked > working > done > unknown > idle).

A repo with extra git worktrees (switched on) has them hidden by default; a ▸ after its name marks them.
→ opens them and ← closes them (← on a worktree, or on a closed repo, goes to its parent row), as in the
files and tasks panels. An open list shows each worktree indented: the branch, with the agent dot when a
window of that worktree is open (a worktree with no window is dim). Enter opens or focuses the worktree's
window. A repo with no extra worktree, or a git error, shows only the repo.

Next to the name, two icons always present:
  branch  = local git: dim without git · normal clean · yellow with uncommitted work
  GitHub  = remote:    dim not on the remote · normal up to date · yellow ahead/behind the upstream
                       (fetch every 5 min)

With "repos_folder" the repos are the git repos of that folder; a repo in no group appears under the
implicit "Sem categoria". F cycles the filter (all, switched on, switched off); T switches the repo
under the cursor on or off ("repos_disabled" in config.yaml). A switched-off repo shows no git state
and opens no window and no agent. Space or a right click opens the repo context menu, built by
src/context_menu.py (move to group, mark as a sibling).
"""
import os
import subprocess
import sys
import time

import context_menu
import glyphs
import lib
import session_state
import worktrees
from i18n import t
from tree_state import Expander
from viewer import BLUE, B, GRAY, DIM, R, GREEN, YELLOW, RED, Row, Viewer


def git_icon(repo):
    """Two icons, always present: branch (local git) and the GitHub logo (remote)."""
    state, dirty = lib.git_state(repo)
    return glyphs.git_icons(state, dirty)


WT_TTL = 10   # seconds between worktree listings: the panel redraws every second, git runs once per TTL
_WT_CACHE = {}   # repo -> (ts, [(branch, path), ...])


def worktrees_of(repo):
    """[(branch, path), ...] of the extra worktrees of `repo`, sorted by branch; the main checkout is
    left out. A git error, or a repo that is not a git repo, gives [] (the repo shows with no children)."""
    now = time.time()
    cached = _WT_CACHE.get(repo)
    if cached and now - cached[0] < WT_TTL:
        return cached[1]
    try:
        entries = worktrees.list_worktrees(repo)
    except worktrees.WorktreeError:
        entries = []
    main = os.path.realpath(repo)
    found = [(entry.get("branch"), entry["path"]) for entry in entries
             if entry.get("path") and os.path.realpath(entry["path"]) != main
             and os.path.isdir(entry["path"])]
    found.sort(key=lambda item: (item[0] is None, item[0] or "", item[1]))
    _WT_CACHE[repo] = (now, found)
    return found


def worktree_mark(repo):
    """▸ or ▾ after a repo's name when it has extra worktrees (list closed or open); nothing otherwise."""
    if not worktrees_of(repo):
        return ""
    return f" {GRAY}{'▾' if WT_OPEN.is_open(repo) else '▸'}{R}"


def worktree_rows(repo, indent):
    """Keyed rows of the extra worktrees of `repo`, under the repo row, only while its list is open. The state
    dot shows only when a window of the worktree holds agent tabs (its own state file, read by session_state)."""
    rows = []
    if not WT_OPEN.is_open(repo):
        return rows
    for branch, path in worktrees_of(repo):
        PARENT[("w", path)] = ("r", repo)
        label = branch or t("repos.worktree.detached")
        if not session_state.tab_states(path):   # no window of this worktree: dim, no state
            rows.append(Row(f"{indent}{GRAY}↳ {label}{R}", ("w", path)))
            continue
        sel = f"{BLUE}{B}" if path == CURRENT else ""
        rows.append(Row(f"{indent}{glyphs.dot(session_state.repo_state(path))} {sel}{label}{R}", ("w", path)))
    return rows


GROUPS = Expander(lib.STATE / "collapsed.json", default_open=True)   # groups start open; the file keeps the closed ones
WT_OPEN = Expander(lib.STATE / "worktrees_open.json", default_open=False)   # repos start closed; the file keeps the open ones
PARENT = {}   # row key -> the row ← moves to (a worktree -> its repo, a repo -> its group); rebuilt on every redraw
CURRENT = lib.current_repo()
FILTERS = ("all", "on", "off")
VIEW = {"filter": "all", "note": "", "note_until": 0.0}   # per panel run: a restart shows every repo again
NOTE_S = 4   # seconds a note stays in the header (the panel redraws every second)


def note(text):
    VIEW["note"], VIEW["note_until"] = text, time.time() + NOTE_S


def switched_off():
    """Names of the folder repos that are off; without a repos folder nothing is switched."""
    return lib.disabled_repos() if lib.repos_folder() else set()


def shown(name, off):
    """Filter: all repos, only the switched-on ones, or only the switched-off ones."""
    f = VIEW["filter"]
    return f == "all" or (f == "off") == (name in off)


def collect(v):
    groups, state = lib.read_repos(), None
    main = lib.read_main()
    state = lib.states_by_repo(groups + ([("", [main])] if main else []))
    off = switched_off()
    header = []   # the REPOS title lives in the panel's top bar (panel_tabs.py repos)
    if lib.repos_folder():
        header.append(f" {DIM}{t('repos.filter.line', filter=t('repos.filter.' + VIEW['filter']))}{R}")
    if time.time() < VIEW["note_until"]:
        header.append(f" {VIEW['note']}")
    header += context_menu.header()   # the menu layer owns its prompt and notice
    body = []
    PARENT.clear()
    if main:   # main repo: outside the groups, always on top, bold in the default foreground (blue when it is this window's repo)
        repo_name, p = main
        color = BLUE if p == CURRENT else ""
        body.append(Row(f" {glyphs.dot(state.get(p, 'idle'))} {color}{B}{repo_name}{R}{worktree_mark(p)} {git_icon(p)}", ("r", p)))
        body += worktree_rows(p, "   ")
    for name, all_repos in groups:
        repos = [(n, p) for n, p in all_repos if shown(n, off)]
        opened = GROUPS.is_open(name)
        arrow = "▾" if opened else "▸"
        # the closed group dot shows the worst state inside
        worst = max((state.get(p, "idle") for _, p in repos), key=lib.PRIORITY.get, default="idle")
        mark = f" {glyphs.dot(worst)}" if not opened and worst != "idle" else ""
        body.append(Row(f" {GRAY}{arrow} {name.upper()}{R}{mark}", ("g", name)))
        if not opened:
            continue
        for repo_name, p in repos:
            PARENT[("r", p)] = ("g", name)
            if repo_name in off:   # a switched-off repo is not worked on: no git state is read for it
                body.append(Row(f"   {GRAY}○ {repo_name} · {t('repos.disabled')}{R}", ("r", p)))
                continue
            sel = f"{BLUE}{B}" if p == CURRENT else ""
            body.append(Row(f"   {glyphs.dot(state.get(p, 'idle'))} {sel}{repo_name}{R}{worktree_mark(p)} {git_icon(p)}", ("r", p)))
            body += worktree_rows(p, "     ")
    return header, body


def on_key(v, k):
    """F cycles the filter; T switches the repo under the cursor; Left/Right fold a group or a repo's worktrees."""
    if k == "f":
        VIEW["filter"] = FILTERS[(FILTERS.index(VIEW["filter"]) + 1) % len(FILTERS)]
        v.redraw.set()
    elif k == "t" and v.key and v.key[0] == "r":
        toggle(v, v.key[1])
    elif k in ("left", "right") and v.key and v.key[0] in ("g", "r", "w"):
        arrow(v, k)


def arrow(v, k):
    """→ opens a repo's worktrees; ← closes them, or moves to the parent row (a worktree's repo, a repo's group).
    On a group, the arrows open and close it."""
    kind, value = v.key
    if kind == "g":
        GROUPS.handle_arrow(k, value)
    elif k == "right":
        if kind == "r" and worktrees_of(value):
            WT_OPEN.open(value)
    elif kind == "r" and WT_OPEN.is_open(value) and worktrees_of(value):
        WT_OPEN.close(value)
    else:
        v.key = PARENT.get(v.key, v.key)
    v.redraw.set()


def toggle(v, path):
    """Switches the repo on or off in config.yaml; the next redraw reads it back (no tmux state, no restart)."""
    name = os.path.basename(path)
    enable = name in lib.disabled_repos()
    try:
        lib.set_repo_enabled(name, enable)
        note(t("repos.toggled", repo=name, state=t("repo.state.on" if enable else "repo.state.off")))
    except ValueError as error:   # config unreadable, or not a repo of the folder
        note(str(error))
    v.redraw.set()
    v._wake()


def enter(v, key, _pressed):
    kind, value = key
    if kind == "g":
        GROUPS.toggle(value)
        v.redraw.set()
    elif not lib.repo_enabled(value):   # a switched-off repo opens nothing: say so in the header
        note(t("repos.open_disabled", repo=os.path.basename(value)))
        v.redraw.set()
    else:
        subprocess.run([lib.WS, "open", value], timeout=20,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def focus_lost(v):
    """Leaving the pane (or window) puts the cursor back on this window's own repo."""
    v.key = ("r", CURRENT)
    v.redraw.set()
    v._wake()


if __name__ == "__main__":
    Viewer(collect, interval=1.0, cursor=True, on_enter=enter, on_key=on_key, initial_key=("r", CURRENT),
           on_focus_lost=focus_lost, actions=context_menu.actions, on_input=context_menu.on_input).run()
