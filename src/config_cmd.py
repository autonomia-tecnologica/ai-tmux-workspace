"""`workspace config` and `workspace config update`: keep what the workspace installed in line with this repo.

`workspace config` shows the effective config: the path of config.yaml and its text.

`workspace config update` compares four things with what this checkout ships and shows each drift:
  1. config.yaml with config/config.example.yaml. Only what is missing is added, by exact text: a key,
     or an item of a list (prompts, options). Nothing the person has is changed or removed. The keys in
     PERSONAL_KEYS (profiles, groups, paths...) are never offered. Each change is identified by its
     kind, path and value; the identities already applied are kept in state/config-offered.json next
     to config.yaml, so a pattern the person removed does not come back.
  2. the installed harness (~/.local/share/ai-tmux-workspace/harness): drift.check() reports it and
     installer.copy_harness refreshes it from the canonical source.
  3. the generated settings of each profile (lib.expected_profile_settings), when manage is on.
  4. the Claude Code hooks: the installer's own step (`bin/workspace install-hooks`), run against a
     temporary HOME, shows whether it would change ~/.claude/settings.json. Applying runs it for real.

Without `--apply` it only shows. With `--apply` it prints the config diff, writes a backup of
config.yaml before changing it, and fixes the rest. A second run changes nothing.

Stdlib only. End-user text comes from i18n/pt-BR.json (`config.*` keys).
"""
import difflib
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

import drift
import installer
import lib
import yaml_min
from i18n import t

ROOT = Path(__file__).resolve().parent.parent
WORKSPACE = ROOT / "bin" / "workspace"
EXAMPLE = ROOT / "config" / "config.example.yaml"
STATE_FILE = "config-offered.json"
PERSONAL_KEYS = ("profiles", "groups", "main", "relations", "parent_folder", "plugins")
_INLINE_EMPTY = re.compile(r"(:)\s*(\[\]|\{\})(\s+#.*)?$")


# --- line helpers (the config is plain text: the file keeps its comments and layout) -------------

def _indent(line):
    return len(line) - len(line.lstrip(" "))


def _is_content(line):
    stripped = line.strip()
    return bool(stripped) and not stripped.startswith("#")


def _is_item(line):
    stripped = line.strip()
    return stripped == "-" or stripped.startswith("- ")


def _child_indent(lines, lo, hi):
    for i in range(lo, hi):
        if _is_content(lines[i]):
            return _indent(lines[i])
    return None


def _last_content(lines, lo, hi):
    for i in range(hi - 1, lo - 1, -1):
        if _is_content(lines[i]):
            return i
    return None


def _key_lines(lines, lo, hi, indent):
    return [i for i in range(lo, hi)
            if _is_content(lines[i]) and _indent(lines[i]) == indent and not _is_item(lines[i])]


def _item_lines(lines, lo, hi, indent):
    return [i for i in range(lo, hi)
            if _is_content(lines[i]) and _indent(lines[i]) == indent and _is_item(lines[i])]


def _block_end(starts, index, hi):
    return starts[index + 1] if index + 1 < len(starts) else hi


def _insert_at(lines, lo, hi):
    """Position right after the last content line of a block (before its trailing comments and blanks)."""
    last = _last_content(lines, lo, hi)
    return lo if last is None else last + 1


def _leading_comments(lines, index, lo):
    """First line of the comment block sitting directly above `index` (no blank line in between)."""
    start = index
    while start - 1 >= lo and lines[start - 1].strip().startswith("#"):
        start -= 1
    return start


def _reindent(lines, delta):
    out = []
    for line in lines:
        if not line.strip():
            out.append("")
        elif delta >= 0:
            out.append(" " * delta + line)
        else:
            out.append(line[min(-delta, _indent(line)):])
    return out


def _open_text(line):
    """`key: []` -> `key:` (keeps a trailing comment): an empty inline value becomes a block."""
    return _INLINE_EMPTY.sub(lambda m: m.group(1) + (m.group(3) or ""), line).rstrip()


# --- the merge ---------------------------------------------------------------------------------

def identity(change):
    """Stable text that names one offered pattern: its kind, its path and its value."""
    return json.dumps([change["kind"], change["path"], change["value"]], ensure_ascii=False, sort_keys=True)


def _change(kind, path, value):
    return {"kind": kind, "path": list(path), "value": value}


class _Plan:
    def __init__(self, lines, offered):
        self.lines = lines
        self.offered = offered
        self.inserts = []        # (position, sequence, lines)
        self.replacements = {}   # index -> new line text
        self.changes = []        # what will be added, in order
        self.manual = []         # paths of inline lists that cannot be extended without rewriting
        self.skipped = 0         # patterns already offered (or removed by the person): not added again

    def add(self, position, lines, change, replace=None):
        if identity(change) in self.offered:
            self.skipped += 1
            return
        self.inserts.append((position, len(self.inserts), lines))
        self.replacements.update(replace or {})
        self.changes.append(change)


def _merge_mapping(plan, example, user_map, example_map, bounds, path):
    u_lo, u_hi, e_lo, e_hi = bounds
    user_lines = plan.lines
    cu = _child_indent(user_lines, u_lo, u_hi)
    ce = _child_indent(example, e_lo, e_hi)
    if ce is None:
        return
    if cu is None:
        cu = 0
    u_starts = _key_lines(user_lines, u_lo, u_hi, cu)
    e_starts = _key_lines(example, e_lo, e_hi, ce)
    u_index = dict(zip(user_map.keys(), u_starts))
    e_index = dict(zip(example_map.keys(), e_starts))
    for key, value in example_map.items():
        if not path and key in PERSONAL_KEYS:
            continue
        here = path + (key,)
        e_i = e_index[key]
        e_end = _block_end(e_starts, e_starts.index(e_i), e_hi)
        if key not in u_index:
            first = _leading_comments(example, e_i, e_lo)
            chunk = example[first:_last_content(example, e_i, e_end) + 1]
            if cu == 0:
                chunk = [""] + chunk
            plan.add(_insert_at(user_lines, u_lo, u_hi), _reindent(chunk, cu - ce), _change("key", here, value))
            continue
        if not isinstance(value, (dict, list)) or not value:
            continue
        u_i = u_index[key]
        user_value = user_map[key]
        if not isinstance(user_value, type(value)):
            continue
        u_end = _block_end(u_starts, u_starts.index(u_i), u_hi)
        if not user_value:
            _open_empty(plan, example, u_i, e_i, e_end, cu - ce, here, value)
        elif isinstance(value, dict):
            _merge_mapping(plan, example, user_value, value, (u_i + 1, u_end, e_i + 1, e_end), here)
        else:
            _merge_list(plan, example, user_value, value, (u_i, u_end, e_i, e_end), here)


def _open_empty(plan, example, u_i, e_i, e_end, delta, path, value):
    last = _last_content(example, e_i + 1, e_end)
    if last is None:
        return
    chunk = _reindent(example[e_i + 1:last + 1], delta)
    plan.add(u_i + 1, chunk, _change("open", path, value), replace={u_i: _open_text(plan.lines[u_i])})


def _merge_list(plan, example, user_list, example_list, bounds, path):
    u_key, u_end, e_key, e_end = bounds
    user_lines = plan.lines
    cu = _child_indent(user_lines, u_key + 1, u_end)
    if cu is None:
        plan.manual.append(".".join(path))
        return
    ce = _child_indent(example, e_key + 1, e_end)
    starts = _item_lines(example, e_key + 1, e_end, ce)
    if len(starts) != len(example_list):
        raise ValueError(f"list {'.'.join(path)} does not read back the same way")
    remaining = list(user_list)
    for index, item in enumerate(example_list):
        if item in remaining:
            remaining.remove(item)
            continue
        stop = starts[index + 1] if index + 1 < len(starts) else e_end
        chunk = example[starts[index]:_last_content(example, starts[index], stop) + 1]
        plan.add(_insert_at(user_lines, u_key + 1, u_end), _reindent(chunk, cu - ce),
                 _change("item", path, item))


def _contains(new, old):
    """True when `new` keeps every value of `old`: lists may only grow at the end, maps may only gain keys."""
    if isinstance(old, dict):
        return isinstance(new, dict) and all(k in new and _contains(new[k], v) for k, v in old.items())
    if isinstance(old, list):
        return isinstance(new, list) and new[:len(old)] == old
    return new == old


def plan_config(user_text, example_text, offered=frozenset()):
    """(new_text, plan) for config.yaml text against the example. Raises ValueError when a text does
    not read as YAML or the change would alter a value the person has. Writes nothing."""
    user_map = yaml_min.loads(user_text) or {}
    example_map = yaml_min.loads(example_text) or {}
    if not isinstance(user_map, dict) or not isinstance(example_map, dict):
        raise ValueError("config root is not a mapping")
    example = example_text.splitlines()
    user_lines = user_text.splitlines()
    plan = _Plan(user_lines, offered)
    _merge_mapping(plan, example, user_map, example_map, (0, len(user_lines), 0, len(example)), ())
    if not plan.inserts and not plan.replacements:
        return user_text, plan
    lines = list(user_lines)
    for index, text in plan.replacements.items():
        lines[index] = text
    for position, _sequence, chunk in sorted(plan.inserts, key=lambda item: (item[0], item[1]), reverse=True):
        lines[position:position] = chunk
    new_text = "\n".join(lines) + "\n"
    if not _contains(yaml_min.loads(new_text), user_map):
        raise ValueError("the change would alter a value that is already in config.yaml")
    return new_text, plan


def _describe(change):
    value = json.dumps(change["value"], ensure_ascii=False)
    if len(value) > 90:
        value = value[:87] + "..."
    path = ".".join(change["path"])
    key = {"key": "config.update.key", "item": "config.update.item", "open": "config.update.open"}[change["kind"]]
    return t(key, path=path, value=value)


# --- registry of offered patterns ---------------------------------------------------------------

def state_path():
    """state/config-offered.json, in the state folder next to config.yaml."""
    return lib.config_path().parent / "state" / STATE_FILE


def load_offered():
    """Identities already offered. A missing file is empty; an unreadable one raises, so nothing is
    added back over a registry that cannot be read."""
    path = state_path()
    if not path.exists():
        return set()
    data = json.loads(path.read_text(encoding="utf-8"))
    offered = data.get("offered") if isinstance(data, dict) else None
    if not isinstance(offered, list) or not all(isinstance(item, str) for item in offered):
        raise ValueError("unexpected format")
    return set(offered)


def save_offered(identities):
    path = state_path()
    merged = sorted(load_offered() | set(identities))
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps({"offered": merged}, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    tmp.replace(path)


# --- the four steps -----------------------------------------------------------------------------

def _config_step(apply):
    """(exit status, pending) for config.yaml against the example."""
    print(t("config.section.config"))
    path = lib.config_path()
    if not path.is_file():
        print(t("config.update.no_file"))
        return 0, False
    user_text = path.read_text(encoding="utf-8")
    example_text = EXAMPLE.read_text(encoding="utf-8")
    try:
        offered = load_offered()
    except (OSError, ValueError) as error:
        print(t("config.update.state_error", path=str(state_path()), error=str(error)))
        return 1, False
    try:
        new_text, plan = plan_config(user_text, example_text, offered)
    except (OSError, ValueError) as error:
        print(t("config.update.not_written", error=str(error)))
        return 1, False
    for change in plan.changes:
        print(_describe(change))
    for manual in plan.manual:
        print(t("config.update.manual", path=manual))
    if plan.skipped:
        print(t("config.update.skipped", count=plan.skipped))
    if not plan.changes:
        print(t("config.update.none"))
        return 0, bool(plan.manual)
    if not apply:
        return 0, True
    print(t("config.update.diff"))
    sys.stdout.writelines(difflib.unified_diff(
        user_text.splitlines(keepends=True), new_text.splitlines(keepends=True),
        fromfile="config.yaml", tofile="config.yaml (novo)"))
    try:
        backup = lib.write_config_text(new_text)
        save_offered(identity(change) for change in plan.changes)
    except OSError as error:
        print(t("config.update.not_written", error=str(error)))
        return 1, True
    print(t("config.update.applied", backup=str(backup) if backup else "-"))
    return 0, False


def _harness_step(apply):
    print(t("config.section.harness"))
    issues = drift.check()
    if not issues:
        print(t("config.harness.none"))
        return 0, False
    for _level, message in issues:
        print(t("config.harness.issue", message=message))
    pending = [level for level, _message in issues if level != "note"]
    if not pending:
        return 0, False
    if not apply:
        return 0, True
    source = installer.harness_source()
    if source is None:
        print(t("config.harness.no_source"))
        return 1, True
    target = drift.harness_dir()
    try:
        warning = installer.copy_harness(source, target)
    except OSError as error:
        print(t("config.harness.failed", error=str(error)))
        return 1, True
    if warning:
        print(t("installer.harness.render_failed",
                path=str(target.parent / "security" / "settings.json"), error=warning))
    still = [message for level, message in drift.check() if level != "note"]
    for message in still:
        print(t("config.harness.still", message=message))
    print(t("config.harness.applied", path=str(target)))
    return (1 if still else 0), bool(still)


def _read_json(path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _profiles_step(apply):
    print(t("config.section.profiles"))
    if not lib.manage_enabled():
        print(t("config.profiles.skipped"))
        return 0, False
    status, drifted = 0, []
    for name in lib.profile_names():
        try:
            expected = lib.expected_profile_settings(name)
        except lib.SettingsError as error:
            print(t("config.profiles.error", name=name, error=str(error)))
            status = 1
            continue
        if expected is None:
            continue
        path = lib.profile_settings_path(name)
        if _read_json(path) != expected:
            drifted.append(name)
            print(t("config.profiles.drift", name=name, path=str(path)))
    if not drifted and status == 0:
        print(t("config.profiles.none"))
        return 0, False
    if not apply:
        return status, bool(drifted)
    for name in drifted:
        try:
            lib.ensure_profile_settings(name)
            print(t("config.profiles.applied", name=name))
        except lib.SettingsError as error:
            print(t("config.profiles.error", name=name, error=str(error)))
            status = 1
    return status, False


def _run_hook_step(env=None, capture=True):
    return subprocess.run([installer.BASH, str(WORKSPACE), "install-hooks"], env=env,
                          capture_output=capture, text=True)


def _hooks_would_change():
    """True when the installer's hook step would change ~/.claude/settings.json. It runs on a copy in a
    temporary HOME, so the real file is never touched by the check."""
    real = Path.home() / ".claude" / "settings.json"
    original = real.read_bytes() if real.is_file() else None
    with tempfile.TemporaryDirectory() as scratch:
        copy = Path(scratch) / ".claude" / "settings.json"
        if original is not None:
            copy.parent.mkdir(parents=True)
            copy.write_bytes(original)
        result = _run_hook_step(env=dict(os.environ, HOME=scratch))
        after = copy.read_bytes() if copy.is_file() else None
    if result.returncode != 0:
        raise RuntimeError((result.stderr or result.stdout or "").strip() or str(result.returncode))
    return after != original


def _hooks_step(apply):
    print(t("config.section.hooks"))
    if not lib.manage_enabled():
        print(t("config.hooks.skipped"))
        return 0, False
    try:
        changed = _hooks_would_change()
    except (OSError, RuntimeError) as error:
        print(t("config.hooks.failed", error=str(error)))
        return 1, False
    if not changed:
        print(t("config.hooks.none"))
        return 0, False
    print(t("config.hooks.drift"))
    if not apply:
        return 0, True
    result = _run_hook_step(capture=False)
    if result.returncode != 0:
        print(t("config.hooks.failed", error=str(result.returncode)))
        return 1, True
    print(t("config.hooks.applied"))
    return 0, False


# --- commands -----------------------------------------------------------------------------------

def show():
    path = lib.config_path()
    print(t("config.show.path", path=str(path)))
    if path.is_file():
        print(path.read_text(encoding="utf-8").rstrip("\n"))
    elif lib.legacy_path().is_file():
        print(t("config.show.legacy", path=str(lib.legacy_path())))
        print(lib.legacy_path().read_text(encoding="utf-8").rstrip("\n"))
    else:
        print(t("config.show.missing"))
    lib.read_config()
    if lib.CONFIG_ERROR:
        print(t("config.show.error", error=str(lib.CONFIG_ERROR)))
        return 1
    return 0


def update(apply):
    status, pending = 0, False
    for step in (_config_step, _harness_step, _profiles_step, _hooks_step):
        code, step_pending = step(apply)
        status = max(status, code)
        pending = pending or step_pending
    if not apply and pending:
        print(t("config.hint"))
    elif apply and status == 0:
        print(t("config.done"))
    return status


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        return show()
    if argv == ["update"]:
        return update(apply=False)
    if argv == ["update", "--apply"]:
        return update(apply=True)
    print(t("config.usage"), file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
