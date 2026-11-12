"""Parser for the unified BACKLOG.md format (see ~/.claude/CLAUDE.md, section Backlog)."""
import re
from pathlib import Path

ITEM = re.compile(r"^###\s+(\d+)\.\s*(.*?)\s*$")
# "a validar" is legacy: backlogs written before the section was dropped still carry it. Its items are
# closed (not listed, their ids count as done), so the panel never shows them and no backlog turns "sem formato".
SECTIONS = {"refinadas": "ready", "a refinar": "refine", "a validar": "validate"}
PRIORITIES = ("P0", "P1", "P2")
DEFAULT_PRIORITY = "P2"


def _paths(text):
    """`Caminhos` as a list: backticks stripped, split on commas and whitespace, empties dropped."""
    return [p for p in re.split(r"[,\s]+", text.replace("`", "")) if p]


def _priority(text):
    """Explicit priority P0/P1/P2 (case-insensitive); absent or invalid is P2."""
    value = text.strip().upper()
    return value if value in PRIORITIES else DEFAULT_PRIORITY


def parse(path):
    """-> None if the file has no recognizable sections, else the list of open items (see document)."""
    doc = document(path)
    return None if doc is None else doc["items"]


def document(path):
    """-> None if the file has no recognizable sections, else {"items": [...], "gone": [int],
    "legacy": [int], "next_id": int|None}. Items: {"id", "title", "category": str ("" if missing),
    "status": ready|refine|paused, "deps": [int], "paths": [str], "priority": P0|P1|P2, "accept": str,
    "blocked": bool, "cycle": bool, "unblocks": int, "typo": [int], "cross": [int]}.
    "gone": ids below "Próximo id" that the file no longer lists (taken as delivered; the skill shows
    them so a typo or a lost item is not mistaken for a delivery). "legacy": ids under the old
    "A validar" section (closed, not listed)."""
    try:
        lines = Path(path).read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    items, section, cur, seen, next_id = [], None, None, False, None
    listed, legacy = set(), []
    for line in lines:
        if line.startswith("## "):
            section = SECTIONS.get(line[3:].strip().lower())
            seen = seen or section is not None
            cur = None
            continue
        m = ITEM.match(line)
        if m and section:
            if section == "validate":           # legacy: closed, so no item is built for it
                legacy.append(int(m.group(1)))
                cur = None
                continue
            title = m.group(2)
            paused = "(PAUSADA)" in title.upper()
            cur = {"id": int(m.group(1)), "title": re.sub(r"\s*\(PAUSADA\)\s*", " ", title, flags=re.I).strip(),
                   "status": "paused" if paused else section, "deps": [],
                   "paths": [], "priority": DEFAULT_PRIORITY, "accept": "", "category": ""}
            items.append(cur)
            listed.add(cur["id"])
        elif cur is not None:
            d = re.match(r"^-\s*Depende de:\s*(.*)$", line)
            if d:
                cur["deps"] = [int(n) for n in re.findall(r"\d+", d.group(1))]
            c = re.match(r"^-\s*Categoria:\s*(.*?)\s*$", line)
            if c:
                cur["category"] = c.group(1)
            p = re.match(r"^-\s*Caminhos:\s*(.*)$", line)
            if p:
                cur["paths"] = _paths(p.group(1))
            pr = re.match(r"^-\s*Prioridade:\s*(.*)$", line)
            if pr:
                raw = pr.group(1).strip()
                cur["priority"] = _priority(raw)
                if raw and raw.upper() not in PRIORITIES:
                    cur["priority_raw"] = raw      # invalid value kept for diagnostics only
            a = re.match(r"^-\s*(?:Aceite(?:\s*\([^)]*\))?|Conferir):\s*(.*)$", line)
            if a:
                cur["accept"] = a.group(1).strip()
        nxt = re.match(r"^Próximo id:\s*(\d+)", line)
        if nxt:
            next_id = int(nxt.group(1))
    if not seen:
        return None
    resolve(items, next_id)
    present = listed | set(legacy)
    gone = [n for n in range(1, next_id or 0) if n not in present]
    return {"items": items, "gone": gone, "legacy": legacy, "next_id": next_id}


def resolve(items, next_id):
    """Adds "blocked" (a listed id is still in the file), "cycle" (item is in a dependency
    cycle, self-dependency included; invalid and blocked), "unblocks" (open items that depend on
    it, directly or transitively), "typo" (ids >= next id) and "cross" (dependencies on an item of
    another category: invalid, categories never depend on each other)."""
    ids = {i["id"] for i in items}
    cat = {i["id"]: i["category"] for i in items}
    graph = {i["id"]: [d for d in i["deps"] if d in ids] for i in items}

    def reaches(start, goal):
        seen, stack = set(), list(graph[start])
        while stack:
            n = stack.pop()
            if n == goal:
                return True
            if n not in seen:
                seen.add(n)
                stack.extend(graph[n])
        return False

    for i in items:
        i["cycle"] = reaches(i["id"], i["id"])
        i["blocked"] = bool(graph[i["id"]]) or i["cycle"]
        i["unblocks"] = sum(1 for j in items if j["id"] != i["id"] and reaches(j["id"], i["id"]))
        i["typo"] = [d for d in i["deps"] if next_id is not None and d >= next_id]
        i["cross"] = [d for d in i["deps"] if d in cat and cat[d] != i["category"]]


def groups(items):
    """-> [(category, [items])] in order of first appearance in the file; items without a
    category (invalid format) go last under the empty name."""
    out = {}
    for i in items:
        out.setdefault(i["category"], []).append(i)
    names = [c for c in out if c] + ([""] if "" in out else [])
    return [(c, out[c]) for c in names]


def _overlaps(a, b):
    """One path covers the other when equal or when it is a directory prefix of the other."""
    a, b = a.rstrip("/"), b.rstrip("/")
    if not a or not b:
        return a == b
    return a == b or a.startswith(b + "/") or b.startswith(a + "/")


def disjoint(a, b):
    """True when no path in `a` overlaps a path in `b` (a prefix counts as overlap, e.g.
    `src/` and `src/x.py`). Empty sets are disjoint."""
    return not any(_overlaps(x, y) for x in set(a) for y in set(b))


def parallel_ids(items):
    """Ids of ready items whose paths are disjoint from at least one other ready item's paths.
    Items without `Caminhos` cannot be declared parallel-safe, so they are left out."""
    ready = [i for i in items if i["status"] == "ready" and not i.get("blocked") and not i.get("cycle")]
    return [i["id"] for i in ready
            if i["paths"] and any(j is not i and j["paths"] and disjoint(i["paths"], j["paths"])
                                  for j in ready)]


def _priority_rank(i):
    p = i.get("priority", DEFAULT_PRIORITY)
    return PRIORITIES.index(p) if p in PRIORITIES else len(PRIORITIES) - 1


def ordered(items):
    """Priority-aware topological order: an item only comes after what it depends on; among the
    items whose dependencies are already placed, explicit priority (P0 first), then `unblocks`
    (larger first), then file order choose the next one. Cycle members go at the end."""
    by_id = {i["id"]: i for i in items}
    pos = {i["id"]: n for n, i in enumerate(items)}
    pending = {i["id"]: {d for d in i["deps"] if d in by_id and not by_id[d].get("cycle")}
               for i in items}
    active = [i for i in items if not i.get("cycle")]
    placed, out = set(), []
    while len(out) < len(active):
        candidates = [i for i in active if i["id"] not in placed and pending[i["id"]] <= placed]
        if not candidates:                       # defensive: never loop on an unforeseen cycle
            candidates = [i for i in active if i["id"] not in placed]
        chosen = min(candidates, key=lambda i: (_priority_rank(i), -i.get("unblocks", 0), pos[i["id"]]))
        placed.add(chosen["id"])
        out.append(chosen)
    out.extend(i for i in items if i.get("cycle"))
    return out


def tree(items):
    """Display rows in dependency-tree order: -> [(item, parent_id|None, is_last, has_children)].
    The parent is the dependency that unblocks the item last; items without a usable parent
    (no deps, cycle, or a dependency that comes later) are roots. Cycle members stay last."""
    out = sorted(ordered(items), key=lambda i: bool(i.get("cycle")))
    pos = {i["id"]: n for n, i in enumerate(out)}
    parent, kids = {}, {}
    for i in out:
        cand = [d for d in i["deps"] if d in pos and pos[d] < pos[i["id"]]]
        p = max(cand, key=pos.get) if cand and not i["cycle"] else None
        parent[i["id"]] = p
        kids.setdefault(p, []).append(i["id"])
    return [(i, parent[i["id"]], kids[parent[i["id"]]][-1] == i["id"], i["id"] in kids) for i in out]
