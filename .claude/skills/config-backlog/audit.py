#!/usr/bin/env python3
"""Read-only audit of the BACKLOG.md rule across the repos in settings.json.

Usage: audit.py [repo-name-or-path ...]   (no args = every configured repo)
Prints one JSON object per repo (JSON lines). Never writes anything.
"""
import json
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "src"))
import backlog  # noqa: E402
import lib  # noqa: E402

LEGACY = ["TODO.md", "TODOS.md", "docs/BACKLOG.md", "docs/TODO.md", "ROADMAP.md", "docs/ROADMAP.md", "TASKS.md"]
FIELDS = {"Categoria", "Arquivos", "Depende de", "Aceite", "Validação"}
VALIDATE_FIELDS = {"Categoria", "Conferir", "Como", "Decide"}


def git(repo, *args):
    r = subprocess.run(["git", "-C", repo, *args], capture_output=True, text=True)
    return r.returncode, r.stdout.strip()


def format_problems(text):
    """Structural checks the panel parser does not do: next id, duplicate ids, missing fields."""
    problems, section, cur, ids = [], None, None, []
    nxt = re.search(r"^Próximo id:\s*(\d+)", text, re.M)
    if not nxt:
        problems.append("missing 'Próximo id: N' at the top")
    order = [m.strip().lower() for m in re.findall(r"^## (.+)$", text, re.M)]
    expected = [s for s in ["refinadas", "a refinar", "a validar"] if s in order]
    if order != expected or len(order) != 3:
        problems.append(f"sections must be exactly: Refinadas, A refinar, A validar (found {order})")
    blocks = re.split(r"^(?=### |## )", text, flags=re.M)
    for b in blocks:
        if b.startswith("## "):
            section = b[3:].splitlines()[0].strip().lower()
            continue
        m = re.match(r"### (\d+)\.\s*(.*)", b)
        if not m:
            if b.startswith("###"):
                problems.append(f"item without numeric id: {b.splitlines()[0][:60]}")
            continue
        n = int(m.group(1))
        ids.append(n)
        have = set(re.findall(r"^- ([^:\n]+):", b, re.M))
        need = VALIDATE_FIELDS if section == "a validar" else FIELDS
        if have & need != need:
            problems.append(f"item {n}: missing {sorted(need - have)}")
        if len(b.strip().splitlines()) > 20:
            problems.append(f"item {n}: longer than ~15 lines")
    if len(ids) != len(set(ids)):
        problems.append("duplicate ids")
    if nxt and ids and max(ids) >= int(nxt.group(1)):
        problems.append(f"'Próximo id' ({nxt.group(1)}) is not above the highest id ({max(ids)})")
    return problems


def audit(name, path):
    out = {"repo": name, "path": path}
    p = Path(path)
    if not p.is_dir():
        return {**out, "error": "path does not exist"}
    is_git = git(path, "rev-parse", "--git-dir")[0] == 0
    out["git"] = is_git
    out["legacy_files"] = [f for f in LEGACY if (p / f).is_file()]
    bl = p / "BACKLOG.md"
    out["backlog"] = bl.is_file()
    if is_git:
        ignored = git(path, "check-ignore", "-q", "BACKLOG.md")[0] == 0
        gi = p / ".gitignore"
        out["ignored"] = ignored
        out["ignored_as_root_rule"] = gi.is_file() and bool(re.search(r"^/BACKLOG\.md\s*$", gi.read_text(errors="ignore"), re.M))
        out["tracked"] = git(path, "ls-files", "--error-unmatch", "BACKLOG.md")[0] == 0
        out["has_remote"] = bool(git(path, "remote")[1])
    if bl.is_file():
        text = bl.read_text(encoding="utf-8", errors="replace")
        items = backlog.parse(bl)
        out["panel_parses"] = items is not None
        out["items"] = len(items or [])
        out["problems"] = format_problems(text)
        out["cross_category"] = {i["id"]: i["cross"] for i in items or [] if i["cross"]}
        out["cycles"] = [i["id"] for i in items or [] if i["cycle"]]
        out["typo_deps"] = {i["id"]: i["typo"] for i in items or [] if i["typo"]}
    out["ok"] = not out.get("error") and (not is_git or (out.get("ignored_as_root_rule") and not out.get("tracked"))) \
        and not out["legacy_files"] and (not bl.is_file() or (out["panel_parses"] and not out["problems"] and not out["cycles"] and not out["cross_category"]))
    return out


def main():
    want = sys.argv[1:]
    for _group, repos in lib.ler_repos():
        for name, path in repos:
            if want and name not in want and path not in [lib.expandir(w) for w in want]:
                continue
            print(json.dumps(audit(name, path), ensure_ascii=False))


if __name__ == "__main__":
    main()
