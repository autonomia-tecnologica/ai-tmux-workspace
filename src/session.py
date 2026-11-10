#!/usr/bin/env python3
"""Session box declared for each agent launch.

The box is the harness policy, not an OS sandbox: the launcher prefixes the agent command with
`WS_SESSION_ROLE`/`WS_SESSION_ROOT`, and the hook/plugin enforce the role table on every tool call.
The role comes from the config's main repo: the main repo is the `home` session (it may change the
other repos) and every other path is a `repo` session whose box is the path itself. A worktree is
just a path, so its box is the worktree, wherever it lives; there is no fixed worktree folder here.

Stdlib only and no `lib`/`tmux` import: the launcher, the hook and the tests use this module without
the rest of the workspace. The config path rule is duplicated from `lib.read_config` on purpose.
"""
import json
import os
import shlex
import sys
from pathlib import Path

try:
    import yaml_min
except Exception:       # installed without the reader: the main repo stays unknown
    yaml_min = None

ROLE = "WS_SESSION_ROLE"
ROOT = "WS_SESSION_ROOT"
HOME_ROLE = "home"
REPO_ROLE = "repo"

USAGE = "usage: session.py env <repo>"


def _config_path():
    """Single config.yaml; AT_WS_CONFIG overrides the path (same rule as lib.config_path)."""
    return Path(os.environ.get("AT_WS_CONFIG", Path.home() / ".config" / "ai-tmux-workspace" / "config.yaml"))


def _legacy_path():
    """Old settings.json, read while config.yaml does not exist (same rule as lib.legacy_path)."""
    if "AT_WS_CONFIG" in os.environ:
        return _config_path().with_suffix(".json")
    return Path.home() / ".config" / "ai-tmux-workspace" / "settings.json"


def _read_config():
    """Config as a dict, or {} when it is absent or cannot be read. Never raises."""
    for path in (_config_path(), _legacy_path()):
        try:
            if not path.is_file():
                continue
            text = path.read_text(encoding="utf-8")
            data = json.loads(text) if path.suffix == ".json" else (yaml_min.loads(text) or {})
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}
    return {}


def _real(value):
    return os.path.realpath(os.path.normpath(os.path.abspath(os.path.expanduser(str(value)))))


def harness_managed():
    """Whether the workspace manages the installed harness/HARNESS.md (default true)."""
    return _read_config().get("harness_managed", True) is not False


def main_repo():
    """Real path of the config's main repo, or None. It is the only `home` session."""
    value = _read_config().get("main")
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return _real(value)
    except (OSError, ValueError):
        return None


def environment(repo, main=None):
    """`{WS_SESSION_ROLE, WS_SESSION_ROOT}` for a session at `repo`, or {} without a repo.

    `main` is the path from `lib.read_main()`; without it the config is read here. The main repo is
    the `home` role and every other path is `repo`. The root is always the real path of `repo`.
    """
    if not isinstance(repo, str) or not repo.strip():
        return {}
    try:
        root = _real(repo)
    except (OSError, ValueError):
        return {}
    if main is None:
        main = main_repo()
    elif isinstance(main, str) and main.strip():
        try:
            main = _real(main)
        except (OSError, ValueError):
            main = None
    else:
        main = None
    return {ROLE: HOME_ROLE if main and root == main else REPO_ROLE, ROOT: root}


def prefix(repo, main=None):
    """`env WS_SESSION_ROLE=... WS_SESSION_ROOT=...` for a repo, or "" without one.

    The `env` form survives both `tmux send-keys` (a shell line) and `shlex.split` + `subprocess`
    (the options popup), so the same launch line works in the two callers.
    """
    values = environment(repo, main)
    if not values:
        return ""
    return "env " + " ".join(f"{name}={shlex.quote(value)}" for name, value in values.items())


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] in ("-h", "--help"):
        print(USAGE)
        return 0
    if len(argv) != 2 or argv[0] != "env":
        print(USAGE, file=sys.stderr)
        return 2
    text = prefix(argv[1])
    if text:
        print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
