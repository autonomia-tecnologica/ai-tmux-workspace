"""Git worktrees: create, list, prepare, merge and remove one worktree per branch.

A worktree lives under `~/.local/share/ai-tmux-workspace/worktrees/<repo>/<branch>`; `WS_WORKTREES`
overrides that root (read at call time, like `WS_STATE` in lib.py), so tests isolate it.

`.workspace.yaml` in the main checkout (optional) drives preparation: `base` (default branch for new
worktrees), `setup` (shell command run inside the worktree), `include` (literal gitignored files to
copy from the main checkout) and `shared` (directories in the main checkout to symlink).
"""
import os
import shutil
import subprocess
import sys
from pathlib import Path

import lib
import paths
import yaml_min
from i18n import t

SETUP_TIMEOUT = 300   # seconds a `.workspace.yaml` setup command may run
GIT_TIMEOUT = 120     # seconds a worktree operation (add, merge) may run


class WorktreeError(RuntimeError):
    """A worktree operation cannot proceed; the message is already user-facing (i18n)."""

    def __init__(self, message, stderr=""):
        super().__init__(message)
        self.stderr = stderr


class MergeConflict(WorktreeError):
    """`git merge` stopped with conflicts; nothing was aborted or cleaned up."""


def root():
    """Worktree root: `WS_WORKTREES` when set, else the standard data dir. Read at call time."""
    env = os.environ.get("WS_WORKTREES")
    if env:
        return Path(os.path.expanduser(env))
    return paths.install_dir() / "worktrees"


def _check_root_outside_folder():
    """Refuses a worktree root inside the repos folder: the folder lists its direct git repos, so the
    worktrees there would read as repos. Only creation checks it; listing and pruning read the root as is."""
    folder = lib.repos_folder()
    if not folder:
        return
    base, folder = os.path.realpath(root()), os.path.realpath(folder)
    if base == folder or base.startswith(folder + os.sep):
        raise WorktreeError(t("worktree.root_in_folder", root=base, folder=folder))


def _expand(value):
    return os.path.realpath(os.path.expanduser(value))


def resolve_repo(value):
    """Path of a repo given as a path or as a name listed in the workspace config."""
    path = _expand(value)
    if os.path.isdir(path):
        return path
    for _, repos in lib.read_repos():
        for name, repo in repos:
            if value in (name, os.path.basename(repo)):
                return repo
    main = lib.read_main()
    if main and value in (main[0], os.path.basename(main[1])):
        return main[1]
    raise WorktreeError(t("worktree.not_a_repo", repo=value))


def _require_repo(repo):
    if lib.git(repo, "rev-parse", "--is-inside-work-tree")[0] != 0:
        raise WorktreeError(t("worktree.not_a_repo", repo=repo))
    return repo


def validate_branch(branch):
    """Branch names that could escape the worktree root or read as git options are refused."""
    parts = branch.replace("\\", "/").split("/") if isinstance(branch, str) else []
    if not isinstance(branch, str) or not branch or branch.startswith("-") \
            or any(c.isspace() for c in branch) or ".." in branch \
            or branch.startswith(("/", "\\")) or any(part in ("", ".", "..") for part in parts):
        raise WorktreeError(t("worktree.bad_branch", branch=str(branch)))
    return branch


def _inside(base, rel):
    """Path of `rel` under `base`, or None when it is absolute or escapes `base` (`..`)."""
    if not isinstance(rel, str) or not rel or os.path.isabs(rel):
        return None
    base = Path(base).resolve()
    candidate = (base / rel).resolve()
    try:
        candidate.relative_to(base)
    except ValueError:
        return None
    return candidate


def path_for(repo, branch):
    """Worktree path: <root>/<repo basename>/<branch> (a branch with '/' nests deeper)."""
    return root() / (os.path.basename(repo) or repo) / validate_branch(branch)


def default_base(repo):
    """`origin/main` when it exists, otherwise `main`, otherwise `master`."""
    for ref in ("origin/main", "main", "master"):
        if lib.git(repo, "rev-parse", "--verify", "--quiet", ref)[0] == 0:
            return ref
    raise WorktreeError(t("worktree.no_base", repo=repo))


def read_config(repo):
    """`.workspace.yaml` from the main checkout as a dict, or None when there is no config."""
    path = Path(repo) / ".workspace.yaml"
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError as error:
        raise WorktreeError(t("worktree.config_invalid", path=str(path), error=error))
    try:
        data = yaml_min.loads(text)
    except yaml_min.YamlError as error:
        raise WorktreeError(t("worktree.config_invalid", path=str(path), error=error))
    return data if isinstance(data, dict) else {}


def _config_base(config):
    base = config.get("base") if isinstance(config, dict) else None
    return base if isinstance(base, str) and base else None


def create(repo, branch, base=None):
    """Creates the worktree for `branch` and returns its path (no preparation, no window)."""
    _check_root_outside_folder()
    repo = _require_repo(resolve_repo(repo))
    validate_branch(branch)
    dest = path_for(repo, branch)
    if dest.exists():
        raise WorktreeError(t("worktree.exists", path=str(dest)))
    base = base or default_base(repo)
    dest.parent.mkdir(parents=True, exist_ok=True)
    rc, _, err = lib.git_full(repo, "worktree", "add", "-b", branch, str(dest), base,
                              timeout=GIT_TIMEOUT)
    if rc != 0:
        raise WorktreeError(t("worktree.create_failed", branch=branch, error=(err or "").strip()),
                            stderr=err)
    return dest


def prepare(repo, worktree, config=None):
    """Applies `.workspace.yaml` (include, shared, setup) to an existing worktree.

    Returns {"included": [...], "shared": [...], "setup": bool}; no config means no preparation.
    The main checkout is where `include`/`shared` read from; `setup` runs with cwd = worktree.
    """
    repo = _expand(repo)
    worktree = Path(worktree)
    config = read_config(repo) if config is None else config
    result = {"included": [], "shared": [], "setup": False}
    if not config:
        return result

    include = config.get("include")
    for rel in include if isinstance(include, list) else []:
        source = _inside(repo, rel)
        if source is None or not source.is_file():
            continue
        if lib.git(repo, "check-ignore", "-q", rel)[0] != 0:
            continue                      # only gitignored files are copied, never a tracked checkout
        target = worktree / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        result["included"].append(rel)

    shared = config.get("shared")
    for rel in shared if isinstance(shared, list) else []:
        source = _inside(repo, rel)
        if source is None or not source.is_dir():
            continue
        target = worktree / rel
        if target.exists() or target.is_symlink():
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        os.symlink(str(source), str(target))
        result["shared"].append(rel)

    setup = config.get("setup")
    if isinstance(setup, str) and setup.strip():
        try:
            run = subprocess.run(setup, shell=True, cwd=str(worktree), capture_output=True,
                                 text=True, timeout=SETUP_TIMEOUT)
        except subprocess.TimeoutExpired:
            raise WorktreeError(t("worktree.setup_timeout", branch=worktree.name,
                                  timeout=SETUP_TIMEOUT))
        if run.returncode != 0:
            detail = (run.stderr or run.stdout or "").strip()
            raise WorktreeError(t("worktree.setup_failed", branch=worktree.name,
                                  error=detail), stderr=detail)
        result["setup"] = True

    return result


def new(repo, branch, base=None):
    """Creates and prepares a worktree; returns (path, preparation result)."""
    repo = resolve_repo(repo)
    config = read_config(repo)
    base = base or _config_base(config)
    dest = create(repo, branch, base)
    return dest, prepare(repo, dest, config)


def list_worktrees(repo):
    """[{"path": ..., "branch": ..., "head": ...}] as `git worktree list --porcelain` reports."""
    repo = _require_repo(resolve_repo(repo))
    rc, out, err = lib.git_full(repo, "worktree", "list", "--porcelain")
    if rc != 0:
        raise WorktreeError(t("worktree.list_failed", error=(err or "").strip()), stderr=err)
    entries, current = [], {}

    def flush():
        if current:
            entries.append(dict(current))
            current.clear()

    for line in out.splitlines():
        if not line:
            flush()
        elif line.startswith("worktree "):
            flush()
            current["path"] = line[len("worktree "):]
        elif line.startswith("branch "):
            current["branch"] = line[len("branch "):].removeprefix("refs/heads/")
        elif line.startswith("HEAD "):
            current["head"] = line[len("HEAD "):]
        elif line == "detached":
            current["branch"] = None
    flush()
    return entries


def _branch_merged(repo, branch):
    return lib.git(repo, "merge-base", "--is-ancestor", branch, "HEAD")[0] == 0


def remove(repo, branch):
    """Removes a clean worktree; deletes the branch only when it is merged into HEAD.

    Returns {"path": ..., "merged": bool, "branch_removed": bool}. A dirty worktree is refused and
    an unmerged branch is preserved (the caller says so).
    """
    repo = _require_repo(resolve_repo(repo))
    validate_branch(branch)
    dest = path_for(repo, branch)
    if not dest.is_dir():
        raise WorktreeError(t("worktree.not_found", path=str(dest)))
    status = lib.git_status(str(dest))
    if status is None:
        raise WorktreeError(t("worktree.not_found", path=str(dest)))
    if status:
        raise WorktreeError(t("worktree.dirty", path=str(dest)))
    rc, _, err = lib.git_full(repo, "worktree", "remove", str(dest))
    if rc != 0:
        raise WorktreeError(t("worktree.remove_failed", error=(err or "").strip()), stderr=err)
    merged = _branch_merged(repo, branch)
    branch_removed = False
    if merged:
        rc, _, err = lib.git_full(repo, "branch", "-d", branch)
        branch_removed = rc == 0
    return {"path": str(dest), "merged": merged, "branch_removed": branch_removed}


def merge(repo, branch):
    """Merges `branch` into the main checkout with `--no-ff`; a conflict raises MergeConflict with
    git's stderr and leaves the repo as is (no abort, no cleanup). Returns git's stdout."""
    repo = _require_repo(resolve_repo(repo))
    rc, out, err = lib.git_full(repo, "merge", "--no-ff", branch, timeout=GIT_TIMEOUT)
    if rc != 0:
        detail = (err or out or "").strip()
        raise MergeConflict(t("worktree.merge_conflict", branch=branch, error=detail), stderr=detail)
    return out.strip()


def all_window_names():
    """Window names of the existing worktrees (the path's basename), so `prune` keeps them.

    Worktrees are the directories under the root that hold a `.git` entry (file or directory).
    """
    names, base = [], root()
    if not base.is_dir():
        return names
    for repo_dir in sorted(base.iterdir()):
        if not repo_dir.is_dir():
            continue
        for dirpath, dirnames, filenames in os.walk(repo_dir):
            if ".git" in dirnames or ".git" in filenames:
                names.append(os.path.basename(dirpath))
                dirnames[:] = []   # a worktree has no nested worktrees under its own root
    return names


def _print_created(dest, result):
    print(t("worktree.created", branch=dest.name, path=str(dest)), file=sys.stderr)
    if result["included"] or result["shared"] or result["setup"]:
        print(t("worktree.prepared", files=len(result["included"]),
                shared=len(result["shared"]), setup=t("worktree.yes") if result["setup"]
                else t("worktree.no")), file=sys.stderr)


def _usage():
    print(t("worktree.usage"), file=sys.stderr)
    return 2


def main(argv):
    """CLI: new <repo> <branch> [base] · list <repo> · remove <repo> <branch> · merge <repo> <branch>."""
    if not argv:
        return _usage()
    command, args = argv[0], argv[1:]
    try:
        if command == "new":
            if len(args) < 2:
                return _usage()
            dest, result = new(args[0], args[1], args[2] if len(args) > 2 else None)
            print(str(dest))            # stdout: the path the bash side opens
            _print_created(dest, result)
        elif command == "list":
            if len(args) != 1:
                return _usage()
            for entry in list_worktrees(args[0]):
                print(f"{entry.get('branch') or '(detached)'}\t{entry['path']}")
        elif command == "remove":
            if len(args) != 2:
                return _usage()
            result = remove(args[0], args[1])
            print(t("worktree.removed", path=result["path"]))
            if not result["merged"]:
                print(t("worktree.unmerged_kept", branch=args[1]))
        elif command == "merge":
            if len(args) != 2:
                return _usage()
            merge(args[0], args[1])
            print(t("worktree.merged", branch=args[1]))
        else:
            return _usage()
    except WorktreeError as error:
        print(error, file=sys.stderr)
        if error.stderr and str(error).find(error.stderr.strip()) < 0:
            print(error.stderr.strip(), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
