#!/usr/bin/env python3
"""Single owner of the workspace paths: config/state, conference and the installed copy.

The layout keeps the person's data and the code apart:

    ~/.config/ai-tmux-workspace/            config.yaml, state/, conference/
    ~/.local/share/ai-tmux-workspace/       app/, harness/, security/

`config_dir()` is the control plane: agents may read it, but no agent role writes it
(`policy.py` checks that before the role table). The environment keeps every historical override
(`WS_STATE`, `WS_CONFERENCE`, `AT_WS_CONFIG`, `AT_HARNESS_DIR`, `AT_SECURITY_DIR`,
`AT_WS_INSTALL_DIR`) ahead of the default.

This is foundation and runs early: stdlib only, no import from `lib`, `session` or `policy`, and
every function reads the environment at call time (tests replace HOME/WS_STATE/AT_WS_CONFIG in the
same process). A failure is reported as one line on stderr; user-facing text belongs to i18n.
"""
import os
import shutil
import sys
from pathlib import Path

CONFIG_DIRNAME = (".config", "ai-tmux-workspace")
INSTALL_DIRNAME = (".local", "share", "ai-tmux-workspace")


def _env_path(name):
    """Path from a non-empty environment variable, or None when the variable is absent/empty."""
    value = os.environ.get(name)
    if value is None or not value.strip():
        return None
    return Path(value).expanduser()


def home():
    return Path.home()


def config_dir():
    """Control plane: the folder of AT_WS_CONFIG, or ~/.config/ai-tmux-workspace."""
    override = _env_path("AT_WS_CONFIG")
    if override is not None:
        return override.parent
    return home().joinpath(*CONFIG_DIRNAME)


def config_path():
    """The single config.yaml; AT_WS_CONFIG overrides the whole path."""
    override = _env_path("AT_WS_CONFIG")
    if override is not None:
        return override
    return config_dir() / "config.yaml"


def legacy_config_path():
    """Old settings.json, read while config.yaml does not exist (same rule as `lib.legacy_path`)."""
    if "AT_WS_CONFIG" in os.environ:
        return config_path().with_suffix(".json")
    return config_dir() / "settings.json"


def state_dir():
    """Runtime state: WS_STATE, or the `state/` folder of the control plane."""
    override = _env_path("WS_STATE")
    if override is not None:
        return override
    return config_dir() / "state"


def settings_dir():
    """Workspace-generated per-profile settings."""
    return state_dir() / "settings"


def conference_dir():
    """Conference folder: WS_CONFERENCE, or the `conference/` folder of the control plane."""
    override = _env_path("WS_CONFERENCE")
    if override is not None:
        return override
    return config_dir() / "conference"


def layout_root():
    """Root of the installed layout (`app/`, `harness/`, `security/`), the one rule for it.

    AT_WS_INSTALL_DIR, or ~/.local/share/ai-tmux-workspace. `install.sh` points AT_WS_INSTALL_DIR at
    the clone (`.../app`), while the layout root is its parent: the harness and the mechanism are
    siblings of the clone, never inside it. `installer`, `drift` and the path helpers use this rule.
    """
    override = _env_path("AT_WS_INSTALL_DIR")
    if override is not None:
        return override.parent if override.name == "app" else override
    return home().joinpath(*INSTALL_DIRNAME)


def install_dir():
    """Installed layout root (see `layout_root`): the parent of `app/`, `harness/` and `security/`."""
    return layout_root()


def harness_dir():
    """Installed harness text: AT_HARNESS_DIR, or `harness/` in the installed copy."""
    override = _env_path("AT_HARNESS_DIR")
    if override is not None:
        return override
    return install_dir() / "harness"


def security_dir():
    """Installed mechanism (`security/`): AT_SECURITY_DIR, the harness sibling, or the default."""
    override = _env_path("AT_SECURITY_DIR")
    if override is not None:
        return override
    installed = _env_path("AT_HARNESS_DIR")
    if installed is not None:
        return installed.parent / "security"
    return install_dir() / "security"


def legacy_state_dir():
    """State of the old layout, migrated once into `state_dir()`."""
    return home() / ".cache" / "workspace"


def _absolute(path):
    return os.path.realpath(os.path.abspath(os.path.expanduser(str(path))))


def _same_or_inside(path, root):
    try:
        left, right = _absolute(path), _absolute(root)
        return os.path.commonpath([left, right]) == right
    except (OSError, ValueError):
        return False


def migrate_state():
    """Move the old `~/.cache/workspace` children into `state_dir()`; return the moved names.

    Idempotent and non-destructive: a declared WS_STATE disables the migration (the person chose
    the state folder), a child already present at the destination is left where it is, and nothing
    is ever moved into the legacy folder itself. An OSError propagates so the caller can warn.
    """
    if _env_path("WS_STATE") is not None:
        return []
    legacy = legacy_state_dir()
    destination = state_dir()
    if not legacy.is_dir() or _same_or_inside(destination, legacy):
        return []
    try:
        destination.mkdir(parents=True, exist_ok=True)
        children = sorted(legacy.iterdir(), key=lambda path: path.name)
        moved = []
        for child in children:
            target = destination / child.name
            if target.exists() or target.is_symlink():
                continue
            shutil.move(str(child), str(target))
            moved.append(child.name)
        return moved
    except OSError as error:
        raise OSError(f"cannot migrate {legacy} to {destination}: {error}") from error


def ensure_layout(migrate=True):
    """Create the control plane, state and conference folders; migrate the old state by default.

    The installed copy is not touched here: creating `app/`, `harness/` or `security/` is the
    installer's job. Returns the names moved by `migrate_state()` (empty when nothing moved).
    """
    for directory in (config_dir(), state_dir(), conference_dir()):
        directory.mkdir(parents=True, exist_ok=True)
    return migrate_state() if migrate else []


USAGE = "usage: paths.py migrate"


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] in ("-h", "--help"):
        print(USAGE)
        return 0
    if argv != ["migrate"]:
        print(USAGE, file=sys.stderr)
        return 2
    try:
        ensure_layout()
    except OSError as error:
        print(f"paths: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
