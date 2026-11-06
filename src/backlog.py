"""Parser for the unified BACKLOG.md format (see ~/.claude/CLAUDE.md, section Backlog)."""
import re
from pathlib import Path

ITEM = re.compile(r"^###\s+(\d+)\.\s*(.*?)\s*$")
SECTIONS = {"refinadas": "ready", "a refinar": "refine", "a validar": "validate"}


def parse(path):
    """-> None if the file has no recognizable sections, else a list of
    {"id", "title", "category": str ("" if missing), "status": ready|refine|validate|paused, "deps": [int],
    "accept": str, "blocked": bool, "cycle": bool, "typo": [int], "cross": [int]}."""
    try:
        lines = Path(path).read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    items, section, cur, seen, next_id = [], None, None, False, None
    for line in lines:
        if line.startswith("## "):
            section = SECTIONS.get(line[3:].strip().lower())
            seen = seen or section is not None
            cur = None
            continue
        m = ITEM.match(line)
        if m and section:
            title = m.group(2)
            paused = "(PAUSADA)" in title.upper()
            cur = {"id": int(m.group(1)), "title": re.sub(r"\s*\(PAUSADA\)\s*", " ", title, flags=re.I).strip(),
                   "status": "paused" if paused and section != "validate" else section, "deps": [], "accept": "", "category": ""}
            items.append(cur)
        elif cur is not None:
            d = re.match(r"^-\s*Depende de:\s*(.*)$", line)
            if d:
                cur["deps"] = [int(n) for n in re.findall(r"\d+", d.group(1))]
            c = re.match(r"^-\s*Categoria:\s*(.*?)\s*$", line)
            if c:
                cur["category"] = c.group(1)
            a = re.match(r"^-\s*(?:Aceite|Conferir):\s*(.*)$", line)
            if a:
                cur["accept"] = a.group(1).strip()
        nxt = re.match(r"^Próximo id:\s*(\d+)", line)
        if nxt:
            next_id = int(nxt.group(1))
    if not seen:
        return None
    resolve(items, next_id)
    return items


def resolve(items, next_id):
    """Adds "blocked" (a listed id is still in the file), "cycle" (item is in a dependency
    cycle, self-dependency included; invalid and blocked), "typo" (ids >= next id) and "cross"
    (dependencies on an item of another category: invalid, categories never depend on each other)."""
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


def ordered(items):
    """Display order: file order, but an item always comes after the items it depends on and
    right after the dependency that unblocks it last. Cycle members go at the end."""
    by_id = {i["id"]: i for i in items}
    placed, out = set(), []

    def ready(i):
        return all(d in placed for d in i["deps"] if d in by_id)

    def place(i):
        placed.add(i["id"])
        out.append(i)
        for j in items:
            if j["id"] not in placed and not j["cycle"] and ready(j):
                place(j)

    for i in items:
        if i["id"] in placed or i["cycle"]:
            continue
        for d in i["deps"]:                      # pull dependencies (and their chain) first
            if d in by_id and d not in placed and not by_id[d]["cycle"]:
                stack = [by_id[d]]
                while stack:
                    x = stack[-1]
                    pend = [by_id[y] for y in x["deps"] if y in by_id and y not in placed
                            and not by_id[y]["cycle"] and by_id[y] is not x and by_id[y] not in stack]
                    if pend:
                        stack.append(pend[0])
                    else:
                        stack.pop()
                        if x["id"] not in placed:
                            place(x)
        if i["id"] not in placed:
            place(i)
    out.extend(i for i in items if i["cycle"])
    return out


def tree(items):
    """Display rows in dependency-tree order: -> [(item, parent_id|None, is_last, has_children)].
    The parent is the dependency that unblocks the item last; items without a usable parent
    (no deps, cycle, or a dependency that comes later) are roots. Items to validate come first."""
    out = sorted(ordered(items), key=lambda i: i["status"] != "validate")   # to validate first (stable)
    pos = {i["id"]: n for n, i in enumerate(out)}
    parent, kids = {}, {}
    for i in out:
        cand = [d for d in i["deps"] if d in pos and pos[d] < pos[i["id"]]]
        p = max(cand, key=pos.get) if cand and not i["cycle"] else None
        parent[i["id"]] = p
        kids.setdefault(p, []).append(i["id"])
    return [(i, parent[i["id"]], kids[parent[i["id"]]][-1] == i["id"], i["id"] in kids) for i in out]
