#!/usr/bin/env python3
"""Tasks panel: open BACKLOG.md items of the repo of this tmux window only.

  ● green ready   ○ dim needs refinement   ⊘ blocked (open dependency or pending decision)   ✖ cycle
A BACKLOG.md outside the unified format shows as "no format"; Enter on an item sends it to Claude; Right/Left expands/collapses its description (▸/▾).
"""
import os
import shutil
import textwrap

import backlog
import glyphs
import i18n
import lib
import tmux
from tree_state import Expander
from viewer import BLUE, B, GRAY, DIM, R, GREEN, RED, YELLOW, Row, Viewer, ellipsize



def t(key, **kw):
    return i18n.t(f"tasks.{key}", **kw)




def effective(i):
    """Display status: a ready item with an open dependency is blocked; cycles are invalid."""
    if i["cycle"]:
        return "cycle"
    return "blocked" if i["blocked"] and i["status"] == "ready" else i["status"]


def buckets(eff):
    """Counter buckets for the header: ready, refine (everything else open), blocked (incl. cycle)."""
    ready = eff.count("ready")
    blocked = sum(e in ("blocked", "cycle") for e in eff)
    refine = len(eff) - ready - blocked
    return {"ready": ready, "refine": refine, "blocked": blocked}


CURRENT = lib.current_repo()
ITEMS = Expander()    # items whose description is expanded (Right opens, Left closes)
GROUPS = Expander()    # categories whose items are shown (Right opens, Left closes); closed by default


def collect(v):
    file = os.path.join(CURRENT, "BACKLOG.md")
    items = backlog.parse(file) if os.path.exists(file) else None
    head = []   # title is in the top bar; the repo name is already in the repos list and the status line
    body = []
    if not os.path.exists(file):
        body.append(Row(f" {DIM}{t('none')}{R}"))
    elif items is None:
        body.append(Row(f" {DIM}{t('no_format')}{R}"))
    else:
        eff = [effective(i) for i in items]
        c = buckets(eff)
        cols = shutil.get_terminal_size((80, 24))[0]
        parts = [(GREEN, c["ready"], "ready"), (GRAY, c["refine"], "refine"), (YELLOW, c["blocked"], "blocked")]
        row, used = "", 0                                   # pack the counters, wrapping when they don't fit
        for color, n, key in parts:
            if key == "blocked" and not n:
                continue
            plain = f"{n} {t(key)}"
            if used and used + 2 + len(plain) + 1 > cols:
                body.append(Row(row))
                row, used = "", 0
            row += ("  " if used else " ") + f"{color}{n}{R} {DIM}{t(key)}{R}"
            used += (2 if used else 1) + len(plain)
        body.append(Row(row))
        body.append(Row())                                # blank line between counters and tasks
        body.append(Row(f" {DIM}{glyphs.arrow(False)}{R} {t('expand_all')}", ("b", CURRENT, "expand")))
        body.append(Row(f" {DIM}{glyphs.arrow(True)}{R} {t('collapse_all')}", ("b", CURRENT, "collapse")))
        body.append(Row())
        rail = {}                                           # id -> tree prefix for its children/lines
        parallel = set(backlog.parallel_ids(items))         # ready items with disjoint paths
        rows = []                                           # (group header | tree row), grouped by category
        for cat, members in backlog.groups(items):
            rows.append(("g", cat, members))
            if GROUPS.is_open(cat):
                rows.extend(("i", *r) for r in backlog.tree(members))
        for row in rows:
            if row[0] == "g":
                _, cat, members = row
                opened = GROUPS.is_open(cat)
                n_ready = sum(effective(m) == "ready" for m in members)
                counts = f"{len(members)}" + (f", {n_ready} {t('ready')}" if n_ready else "")
                name = cat or t("no_category")
                room = cols - len(counts) - 7
                name = ellipsize(name, room)
                body.append(Row(f" {DIM}{glyphs.arrow(opened)}{R} {B}{name}{R} {DIM}({counts}){R}",
                                  ("g", CURRENT, cat)))
                continue
            _, i, parent, last, kids = row
            st = effective(i)
            key = ("i", CURRENT, i["id"], st)
            base = rail.get(parent, " ") if parent is not None else " "
            lead = base + (("└─ " if last else "├─ ") if parent is not None else "")
            rail[i["id"]] = base + (("   " if last else "│  ") if parent is not None else "")
            deps = ", ".join(map(str, i["deps"]))
            dep = f" ←{deps}" if len(i["deps"]) > 1 or (i["deps"] and parent is None) else ""
            decision = st == "blocked" and i["status"] == "blocked"     # Status: blocked: <motivo>
            if decision:                                # short here; the reason shows when expanded
                dep += f" {t('pending')}"
            if i["typo"] or i["cross"]:
                dep += f" {glyphs.warn()}"
            pad = len(lead) + 5 + len(str(i["id"]))   # "<lead>▸ ● 12 "
            badge_plain, badge = "", ""
            priority = i.get("priority", "P2")
            if priority in ("P0", "P1"):               # P2 is the default, so it stays implicit
                label = t("priority", p=priority)
                badge_plain += label + " "
                badge += f"{RED if priority == 'P0' else YELLOW}{label}{R} "
            if i.get("unblocks"):
                label = t("unblocks", n=i["unblocks"])
                badge_plain += label + " "
                badge += f"{DIM}{label}{R} "
            if i["id"] in parallel:
                label = t("parallel")
                badge_plain += label + " "
                badge += f"{GREEN}{label}{R} "
            pad += len(badge_plain)
            opened = ITEMS.is_open(i["id"])
            first = f"{DIM}{lead}{glyphs.arrow(opened)}{R} {glyphs.task_mark(st)} {DIM}{i['id']}{R} {badge}"
            if not opened:
                title = i["title"]
                room = cols - pad - len(dep) - 1
                title = ellipsize(title, room)
                body.append(Row(f"{first}{title}{DIM}{dep}{R}", key))
                continue
            # selected item: full title wrapped, plus deps and acceptance
            width = max(10, cols - pad - 1)
            lines = textwrap.wrap(i["title"], width) or [""]
            body.append(Row(f"{first}{B}{lines[0]}{R}", key))
            ind = f"{DIM}{rail[i['id']]}{'│' if kids else ' '}{R}" + " " * (pad - len(rail[i["id"]]) - 1)
            for ln in lines[1:]:
                body.append(Row(f"{ind}{B}{ln}{R}", cont=True))
            if i["deps"]:
                body.append(Row(f"{ind}{DIM}←{deps}{R}", cont=True))
            if decision:
                body.append(Row(f"{ind}{glyphs.task_mark('blocked')} {t('decision', reason=i['status_reason'])}", cont=True))
            if i["cycle"]:
                body.append(Row(f"{ind}{glyphs.cross()} {t('cycle')}{R}", cont=True))
            if i["cross"]:
                body.append(Row(f"{ind}{YELLOW}{glyphs.warn()} {t('cross', ids=', '.join(map(str, i['cross'])))}{R}", cont=True))
            if i["typo"]:
                body.append(Row(f"{ind}{YELLOW}{glyphs.warn()} {t('typo', ids=', '.join(map(str, i['typo'])))}{R}", cont=True))
            for n, ln in enumerate(textwrap.wrap(i["accept"], width - 2) if i["accept"] else []):
                body.append(Row(f"{ind}{DIM}{'✓ ' if not n else '  '}{ln}{R}", cont=True))
    return head, body


def enter(v, key, k):
    path = key[1]
    if key[0] == "b":          # expand/collapse all categories (collapse also closes item details)
        file = os.path.join(CURRENT, "BACKLOG.md")
        items = backlog.parse(file) if os.path.exists(file) else None
        if key[2] == "expand":
            for cat, _ in backlog.groups(items or []):
                GROUPS.open(cat)
        else:
            for cat, _ in backlog.groups(items or []):
                GROUPS.close(cat)
            for i in items or []:
                ITEMS.close(i["id"])
        v.redraw.set()
        return
    if key[0] == "g":          # category header: Right/Left open and close it
        return
    if key[3] == "cycle":      # invalid until the cycle is undone
        return
    items = backlog.parse(os.path.join(CURRENT, "BACKLOG.md")) or []
    item = next((i for i in items if i["id"] == key[2]), {})
    if key[3] == "blocked" and item.get("status") == "blocked":
        # pending decision (Status: blocked: <motivo>): ask the user about it, never implement
        msg = i18n.t("prompt.tasks.decision", id=key[2], reason=item.get("status_reason", ""))
    else:
        # ready -> implement; blocked by dependency -> implement its open dependencies first;
        # anything else (to refine) -> refine
        kinds = {"ready": "ready", "blocked": "blocked"}
        msg = i18n.t("prompt.tasks." + kinds.get(key[3], "refine"), id=key[2])
    tmux.confirm_then(t("confirm", msg=msg), [lib.WS, "send", path, msg])


def on_key(v, k):
    """Right expands the item or category under the cursor, Left collapses it (like the files tree)."""
    if k not in ("left", "right") or not v.key or v.key[0] not in ("i", "g"):
        return
    (GROUPS if v.key[0] == "g" else ITEMS).handle_arrow(k, v.key[2])
    v.redraw.set()


if __name__ == "__main__":
    Viewer(collect, interval=3.0, cursor=True, on_enter=enter, on_key=on_key, initial_key=None).run()
