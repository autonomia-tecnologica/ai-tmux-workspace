"""`workspace update`: show, apply or roll back the update of the workspace.

Two places can run it. The installed clone (app/, the one the curl install made) is updated by git: a
fetch, then a fast-forward, never a merge. A dev checkout has no such clone to move: `--apply` only
carries its harness, config, settings and hooks to the installed copy (config_cmd), with no git.

    workspace update                show: the installed and remote revision, the new commits, and what changes in
                                    code, config, harness, settings and hooks. Changes nothing.
    workspace update --apply        fetch, check, fast-forward; then `config update --apply` and `reload` run from
                                    the new code. A failing step undoes the whole update.
    workspace update --rollback     back to the revision the last applied update left; then the same two steps.

The revision before an applied update is kept in state/update-previous.json (paths.state_dir()). Before any
step changes something, the installed config, harness, settings and hooks are copied to state/update-snapshot,
so a failure in the middle can put them back. Stdlib only; end-user text comes from i18n/pt-BR.json (`update.*`).
"""
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import config_cmd
import drift
import installer
import lib
import lifecycle
import paths
from i18n import t

ROOT = Path(__file__).resolve().parent.parent
WORKSPACE = ROOT / "bin" / "workspace"
DEFAULT_REPO_URL = "https://github.com/autonomia-tecnologica/ai-tmux-workspace.git"
RECORD_FILE = "update-previous.json"
SNAPSHOT_DIR = "update-snapshot"
# Files whose change means a running Claude Code session or OpenCode plugin must be restarted.
HOOK_FILES = frozenset({"src/hook.py", "src/statusline.py", "security/gate.py", "security/opencode-policy.js"})
GIT_TIMEOUT = 300


class Stop(Exception):
    """A check failed before anything changed. `key` is the i18n text that says what to do."""

    def __init__(self, key, **values):
        super().__init__(key)
        self.key = key
        self.values = values


class StepFailed(Exception):
    """A step of the transaction failed after something may have changed: everything is undone."""

    def __init__(self, key, **values):
        super().__init__(key)
        self.key = key
        self.values = values


# --- where things are ---------------------------------------------------------------------------

def clone_dir():
    """The installed clone: AT_WS_INSTALL_DIR when set (install.sh's rule), else state of the layout's app/."""
    value = os.environ.get("AT_WS_INSTALL_DIR", "").strip()
    return Path(value).expanduser() if value else paths.install_dir() / "app"


def is_installed_clone():
    return os.path.realpath(ROOT) == os.path.realpath(clone_dir())


def expected_remote():
    return os.environ.get("AT_WS_REPO_URL", "").strip() or DEFAULT_REPO_URL


def same_remote(actual, expected):
    def normal(url):
        return url.strip().rstrip("/").removesuffix(".git")
    return normal(actual) == normal(expected)


def record_path():
    return paths.state_dir() / RECORD_FILE


def snapshot_path():
    return paths.state_dir() / SNAPSHOT_DIR


def read_record():
    """{previous, target} of the last applied update, or None when there is none (or it is unreadable)."""
    path = record_path()
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or not isinstance(data.get("previous"), str) or not isinstance(data.get("target"), str):
        return None
    return data


def write_record(previous, target):
    path = record_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps({"previous": previous, "target": target}, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


# --- git ----------------------------------------------------------------------------------------

def git(clone, *args):
    """(exit status, stdout, stderr) of `git -C clone ...`. Never prompts; a missing git is status 127."""
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0")
    try:
        result = subprocess.run(["git", "-C", str(clone), *args], capture_output=True, text=True,
                                env=env, check=False, timeout=GIT_TIMEOUT)
    except subprocess.TimeoutExpired:
        return 124, "", "timeout"
    except OSError as error:
        return 127, "", str(error)
    return result.returncode, result.stdout, result.stderr


def last_line(text):
    lines = [line for line in (text or "").strip().splitlines() if line.strip()]
    return lines[-1].strip() if lines else "-"


def revision(clone, name):
    rc, out, _ = git(clone, "rev-parse", "--verify", "--quiet", f"{name}^{{commit}}")
    return out.strip() if rc == 0 else None


def is_ancestor(clone, older, newer):
    rc, _, _ = git(clone, "merge-base", "--is-ancestor", older, newer)
    return rc == 0


def describe(clone, name):
    _, out, _ = git(clone, "log", "-1", "--format=%h %s", name)
    return out.strip() or name


def changed_files(clone, older, newer):
    _, out, _ = git(clone, "diff", "--name-only", older, newer)
    return [line.strip() for line in out.splitlines() if line.strip()]


def new_commits(clone, older, newer):
    _, out, _ = git(clone, "log", "--format=%h %s", f"{older}..{newer}")
    return [line for line in out.splitlines() if line.strip()]


def category_of(path):
    if path in HOOK_FILES:
        return "hooks"
    for prefix, name in (("config/", "config"), ("harness/", "harness"), ("security/", "settings")):
        if path.startswith(prefix):
            return name
    return "code"


CATEGORY_ORDER = ("config", "harness", "settings", "hooks", "code")


def group_changes(files):
    groups = {name: [] for name in CATEGORY_ORDER}
    for path in files:
        groups[category_of(path)].append(path)
    return groups


def check_clean(clone):
    rc, out, _ = git(clone, "status", "--porcelain")
    if rc != 0:
        raise Stop("update.not_git", path=str(clone))
    if out.strip():
        raise Stop("cli.update.dirty", root=str(clone))


def preflight(clone):
    """The checks an update needs before it moves anything: a clean git clone, the expected remote, a fetch
    that works, an upstream, and a history that can fast-forward. Returns (local, remote)."""
    if not (clone / ".git").exists():
        raise Stop("update.not_git", path=str(clone))
    check_clean(clone)
    rc, url, _ = git(clone, "remote", "get-url", "origin")
    expected = expected_remote()
    if rc != 0 or not same_remote(url, expected):
        raise Stop("update.remote", actual=url.strip() or "-", expected=expected, path=str(clone))
    rc, _, err = git(clone, "fetch", "--quiet", "origin")
    if rc != 0:
        raise Stop("update.fetch_failed", error=last_line(err))
    rc, upstream, _ = git(clone, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}")
    if rc != 0:
        raise Stop("update.no_upstream", path=str(clone))
    local = revision(clone, "HEAD")
    remote = revision(clone, upstream.strip())
    if local is None or remote is None:
        raise Stop("update.no_upstream", path=str(clone))
    if local != remote and not is_ancestor(clone, local, remote):
        raise Stop("update.diverged", path=str(clone))
    return local, remote


# --- snapshot and undo --------------------------------------------------------------------------

def snapshot_targets():
    """(name, path, is_tree) of what an update may change in the installed copy. Only these are saved."""
    harness = drift.harness_dir()
    return [
        ("config.yaml", lib.config_path(), False),
        ("config-offered.json", config_cmd.state_path(), False),
        ("harness", harness, True),
        ("security", harness.parent / "security", True),
        ("settings", paths.settings_dir(), True),
        ("claude-settings.json", Path.home() / ".claude" / "settings.json", False),
    ]


def _present(path, is_tree):
    return path.is_dir() if is_tree else path.is_file()


def _remove(path, is_tree):
    if not (path.exists() or path.is_symlink()):
        return
    if is_tree and path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    else:
        path.unlink()


def take_snapshot(destination):
    if destination.exists():
        shutil.rmtree(destination)
    destination.mkdir(parents=True)
    existing = []
    for name, path, is_tree in snapshot_targets():
        if not _present(path, is_tree):
            continue
        existing.append(name)
        if is_tree:
            shutil.copytree(path, destination / name, symlinks=True)
        else:
            shutil.copy2(path, destination / name)
    (destination / "manifest.json").write_text(json.dumps(existing) + "\n", encoding="utf-8")


def restore_snapshot(source):
    existing = json.loads((source / "manifest.json").read_text(encoding="utf-8"))
    for name, path, is_tree in snapshot_targets():
        _remove(path, is_tree)
        if name not in existing:
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        if is_tree:
            shutil.copytree(source / name, path, symlinks=True)
        else:
            shutil.copy2(source / name, path)


def drop_snapshot():
    shutil.rmtree(snapshot_path(), ignore_errors=True)


# --- running the new code -----------------------------------------------------------------------

def run_workspace(*args):
    """Runs bin/workspace of the running code (after a fast-forward, that is the new code). Returns the status."""
    sys.stdout.flush()
    sys.stderr.flush()
    return subprocess.run([installer.BASH, str(WORKSPACE), *args], check=False).returncode


def preview_config():
    """What `config update` (no --apply) would change now, from the running code. Returns its status."""
    return run_workspace("config", "update")


def hooks_drift():
    """True when the Claude hooks the installer would register differ from ~/.claude/settings.json."""
    if not lib.manage_enabled():
        return False
    try:
        return config_cmd.hooks_would_change()
    except (OSError, RuntimeError):
        return False


# --- the transaction ----------------------------------------------------------------------------

def transaction(clone, original, move, record):
    """Moves the clone with `move` (git args, or None for dev), then syncs config, harness, settings and hooks,
    reloads, and calls `record()` (the revision file, or its removal). Any failing step restores the snapshot
    and, when the clone moved, the revision `original`. Returns the status; prints ok/falhou per step.
    """
    try:
        take_snapshot(snapshot_path())
    except OSError as error:
        print(t("update.snapshot_failed", error=str(error)))
        return 1
    moved = False
    try:
        if move is not None:
            rc, _, err = git(clone, *move)
            if rc != 0:
                raise StepFailed("update.git.failed", error=last_line(err))
            moved = True
            print(t("update.git.ok", rev=describe(clone, "HEAD")))
        if run_workspace("config", "update", "--apply") != 0:
            raise StepFailed("update.config.failed")
        print(t("update.config.ok"))
        if lifecycle.live_profiles():
            if run_workspace("reload") != 0:
                raise StepFailed("update.reload.failed")
            print(t("update.reload.ok"))
        else:
            print(t("update.reload.none"))
        if record is not None:
            try:
                record()
            except OSError as error:
                raise StepFailed("update.record.failed", error=str(error))
    except StepFailed as failure:
        print(t(failure.key, **failure.values))
        undo(clone, original, moved)
        return 1
    drop_snapshot()
    return 0


def undo(clone, original, moved):
    try:
        restore_snapshot(snapshot_path())
        if moved:
            rc, _, err = git(clone, "reset", "--keep", original)
            if rc != 0:
                raise OSError(last_line(err))
        drop_snapshot()
        print(t("update.undone.rev", rev=original[:7]) if moved else t("update.undone"))
    except (OSError, ValueError) as error:
        print(t("update.undo_failed", error=str(error), path=str(clone)))


# --- commands -----------------------------------------------------------------------------------

def show_installed(clone):
    print(t("update.mode.installed", path=str(clone)))
    local, remote = preflight(clone)
    print(t("update.revisions", local=describe(clone, local), remote=describe(clone, remote)))
    if local == remote:
        print(t("update.uptodate"))
    else:
        commits = new_commits(clone, local, remote)
        print(t("update.commits", count=len(commits)))
        for line in commits[:40]:
            print(f"  {line}")
        _print_changes(changed_files(clone, local, remote))
    status = preview_config()
    if local != remote:
        print(t("update.hint"))
    return status


def _print_changes(files):
    groups = group_changes(files)
    if not files:
        print(t("update.changes.none"))
        return
    for name in CATEGORY_ORDER:
        if groups[name]:
            print("  " + t(f"update.changes.{name}", files=", ".join(groups[name])))


def show_dev():
    print(t("update.mode.dev", path=str(ROOT)))
    return preview_config()


def apply_installed(clone):
    print(t("update.mode.installed", path=str(clone)))
    local, remote = preflight(clone)
    moving = local != remote
    hooks_changed = False
    if moving:
        hooks_changed = any(path in HOOK_FILES for path in changed_files(clone, local, remote))
        print(t("update.revisions", local=describe(clone, local), remote=describe(clone, remote)))
        _print_changes(changed_files(clone, local, remote))
    else:
        print(t("update.uptodate"))
    hooks_changed = hooks_changed or hooks_drift()
    move = ["merge", "--ff-only", remote] if moving else None
    record = (lambda: write_record(local, remote)) if moving else None
    status = transaction(clone, local, move, record)
    if status != 0:
        return status
    if moving:
        print(t("update.done", rev=describe(clone, remote)))
    else:
        print(t("update.dev.done"))
    if hooks_changed:
        print(t("update.restart"))
    return 0


def apply_dev():
    print(t("update.mode.dev", path=str(ROOT)))
    hooks_changed = hooks_drift()
    status = transaction(ROOT, "", None, None)
    if status != 0:
        return status
    print(t("update.dev.done"))
    if hooks_changed:
        print(t("update.restart"))
    return 0


def rollback_installed(clone):
    print(t("update.mode.installed", path=str(clone)))
    record = read_record()
    if record is None:
        raise Stop("update.rollback.none")
    check_clean(clone)
    head = revision(clone, "HEAD")
    if head != record["target"]:
        raise Stop("update.rollback.moved", rev=record["target"][:7])
    previous = revision(clone, record["previous"])
    if previous is None:
        raise Stop("update.rollback.none")
    hooks_changed = any(path in HOOK_FILES for path in changed_files(clone, previous, head))
    hooks_changed = hooks_changed or hooks_drift()
    status = transaction(clone, head, ["reset", "--keep", previous], lambda: record_path().unlink(missing_ok=True))
    if status != 0:
        return status
    print(t("update.rollback.done", rev=previous[:7]))
    if hooks_changed:
        print(t("update.restart"))
    return 0


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv not in ([], ["--apply"], ["--rollback"]):
        print(t("update.usage"), file=sys.stderr)
        return 2
    installed = is_installed_clone()
    clone = clone_dir()
    try:
        if argv == ["--rollback"]:
            if not installed:
                raise Stop("update.rollback.dev")
            return rollback_installed(clone)
        if argv == ["--apply"]:
            return apply_installed(clone) if installed else apply_dev()
        return show_installed(clone) if installed else show_dev()
    except Stop as stop:
        print(t(stop.key, **stop.values), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
