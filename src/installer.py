#!/usr/bin/env python3
"""Interactive installer for ai-tmux-workspace, and its killswitch.

`install.sh` checks python3 and calls this assistant; `remove.sh` calls it with `--remove`.
Every step asks for explicit confirmation, every path it touches is recorded in a manifest
with a backup, and privileged commands are only shown: the system steps that ask for sudo
belong to backlog item 35, so this installer never runs sudo/pkexec or installs packages.
When a system step does run one (`--privileged`, also `workspace privileged`), it goes through
tmux.privileged (item 61): a separate tmux screen with the full command visible first, the output
kept until the person closes it, and the exit code returned. Outside tmux nothing runs.

Stdlib only. All end-user text comes from i18n/pt-BR.json (`installer.*` and `privileged.*` keys).

Path overrides (also used by the tests): HOME, AT_WS_CONFIG, WS_STATE,
AT_HARNESS_SOURCE (disables the default harness search when set), AT_WS_INSTALL_STATE, SHELL,
AT_WS_HARNESS_URL (harness clone URL, tests only), AT_WS_PLATFORM (tests only: pretend to be
another OS for the macOS hints) and AT_WS_INSTALL_DIR / AT_WS_REPO_URL (install.sh).
"""
import copy
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

import colors
import tmux
import yaml_min
import paths
from i18n import t

ROOT = Path(__file__).resolve().parent.parent
HOME = Path(os.environ.get("HOME") or Path.home())

# Minimum versions checked by the dependency step. The launcher is POSIX/bash-3.2 safe, so the
# macOS system bash is enough; tmux 3.4 is required by the status bar (range/mouse_status_range),
# and the code uses str.removeprefix (Python 3.9).
MIN_VERSIONS = {"python3": (3, 9), "tmux": (3, 4), "bash": (3, 2)}
HOMEBREW_BOOTSTRAP = '/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"'
# Output pattern per probe; None means the tool has no version check here.
VERSION_PROBES = {
    "python3": (("-c", "import sys; print('%d.%d' % sys.version_info[:2])"), r"(\d+)\.(\d+)"),
    "tmux": (("-V",), r"tmux\s+(?:next-)?(\d+)\.(\d+)"),
    "bash": (("--version",), r"version\s+(\d+)\.(\d+)"),
}


def format_version(version):
    return ".".join(str(part) for part in version)


def command_version(argv, pattern):
    """Version tuple parsed from a command's output, or None when it cannot run or parse."""
    try:
        result = subprocess.run(list(argv), capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    match = re.search(pattern, (result.stdout or "") + (result.stderr or ""))
    if not match:
        return None
    return tuple(int(part) for part in match.groups())


def tool_version(name, path):
    probe = VERSION_PROBES.get(name)
    if probe is None:
        return None
    args, pattern = probe
    return command_version([path, *args], pattern)


def bash_candidates():
    """A usable bash from PATH first, then /bin/bash (never a hardcoded Homebrew path)."""
    seen = set()
    for directory in os.environ.get("PATH", "").split(os.pathsep):
        if not directory:
            continue
        candidate = Path(directory) / "bash"
        if not candidate.is_file() or not os.access(candidate, os.X_OK):
            continue
        key = os.path.realpath(str(candidate))
        if key not in seen:
            seen.add(key)
            yield candidate
    fallback = Path("/bin/bash")
    if fallback.is_file() and os.access(fallback, os.X_OK):
        key = os.path.realpath(str(fallback))
        if key not in seen:
            yield fallback


def find_bash():
    """First bash in PATH (then /bin/bash) that runs and meets MIN_VERSIONS['bash']."""
    minimum = MIN_VERSIONS["bash"]
    older = None
    for candidate in bash_candidates():
        version = tool_version("bash", candidate)
        if version is None:
            continue
        if version >= minimum:
            return str(candidate)
        if older is None:
            older = str(candidate)
    return older or (shutil.which("bash") or "bash")


BASH = find_bash()
# AT_WS_HARNESS_URL replaces the harness repo URL. Only for tests and offline checks: it is never set by
# the install scripts, so a person installing gets the public repo.
CLONE_URL = os.environ.get("AT_WS_HARNESS_URL") or "https://github.com/autonomia-tecnologica/ambiente-de-trabalho.git"
SOURCE_FILE = "source.json"
# Installed next to the harness text: the gate, the policy/config pair and the two adapters' shared
# engine. The source tree (ambiente-de-trabalho) is copied first; these come from this repository.
POLICY_SOURCES = (
    ("security", "gate.py", "gate.py"),
    ("security", "policy.yaml", "policy.yaml"),
    ("security", "opencode-policy.js", "opencode-policy.js"),
    ("src", "policy.py", "policy.py"),
    ("src", "paths.py", "paths.py"),
    ("src", "yaml_min.py", "yaml_min.py"),
    ("i18n", "pt-BR.json", "i18n/pt-BR.json"),
)
CLI_DIR = Path(".local", "bin")
REQUIRED = ("tmux", "python3", "git", "claude")
OPTIONAL = ("gh", "nvim", "micro")
MANIFEST_VERSION = 1

# Item 107: the provider list is exactly these five, in this canonical order (the first one marked
# is the suggested default). Only `claude` uses the Claude harness; every external provider runs
# through OpenCode and takes its credential from the OpenCode auth store, never from this config.
PROVIDERS = ("claude", "opencode-claude", "opencode-deepseek", "opencode-openrouter", "opencode-free")
PROVIDER_PROFILES = {
    "claude": "claude",
    "opencode-claude": {
        "harness": "opencode",
        "command": "opencode",
        "config": "~/.config/ai-tmux-workspace/opencode-claude.json",
        "model": "anthropic/claude-sonnet-5-5",
    },
    "opencode-deepseek": {
        "harness": "opencode",
        "command": "opencode",
        "config": "~/.config/ai-tmux-workspace/opencode-deepseek.json",
        "model": "deepseek/deepseek-flash",
    },
    "opencode-openrouter": {
        "harness": "opencode",
        "command": "opencode",
        "config": "~/.config/ai-tmux-workspace/opencode-openrouter.json",
        "model": "openrouter/deepseek/deepseek-v4-flash-0731",
    },
    "opencode-free": {
        "harness": "opencode",
        "command": "opencode",
        "config": "~/.config/ai-tmux-workspace/opencode-free.json",
        "model": "opencode/deepseek-v4.1-flash-free",
    },
}

# Item 107: names from the earlier catalog, migrated once in an existing config. Only these keys
# move (the profile value, including its `config` path, stays as it is); every other key is kept.
LEGACY_PROFILE_RENAMES = {
    "opencode": "opencode-deepseek",
    "openrouter": "opencode-openrouter",
}
LEGACY_DEEPSEEK = "deepseek"
LEGACY_DEEPSEEK_TARGET = "opencode-deepseek"
LEGACY_DEEPSEEK_HOST = "deepseek.com"

# Package names per manager; only shown to the person, never executed by the assistant.
PACKAGE_NAMES = {
    "pacman": {"tmux": "tmux", "bash": "bash", "git": "git", "python3": "python", "gh": "github-cli", "nvim": "neovim", "micro": "micro"},
    "apt-get": {"tmux": "tmux", "bash": "bash", "git": "git", "python3": "python3", "gh": "gh", "nvim": "neovim", "micro": "micro"},
    "dnf": {"tmux": "tmux", "bash": "bash", "git": "git", "python3": "python3", "gh": "gh", "nvim": "neovim", "micro": "micro"},
    "zypper": {"tmux": "tmux", "bash": "bash", "git": "git", "python3": "python3", "gh": "gh", "nvim": "neovim", "micro": "micro"},
    # The Homebrew Python formula is `python`, not `python3`.
    "brew": {"tmux": "tmux", "bash": "bash", "git": "git", "python3": "python", "gh": "gh", "nvim": "neovim", "micro": "micro"},
}
CLAUDE_INSTALLER = "curl -fsSL https://claude.ai/install.sh | bash"

ZSH_COMPLETION = """#compdef workspace
# ai-tmux-workspace autocomplete (managed by install.sh)
_workspace_completion() {
  local -a commands
  commands=(
__COMMANDS__
  )
  _describe -t commands 'workspace' commands
}
if (( $+functions[compdef] )); then
  compdef _workspace_completion workspace
else
  autoload -Uz compinit && compinit -C
  compdef _workspace_completion workspace
fi
"""

BASH_COMPLETION = """# ai-tmux-workspace autocomplete (managed by install.sh)
_workspace_completion() {
  local cur
  cur="${COMP_WORDS[COMP_CWORD]}"
  if [ "$COMP_CWORD" -eq 1 ]; then
    COMPREPLY=( $(compgen -W "open install install-hooks restart reload fit stop" -- "$cur") )
  elif [ "${COMP_WORDS[1]}" = "open" ]; then
    COMPREPLY=( $(compgen -d -- "$cur") )
  fi
}
complete -F _workspace_completion workspace
"""

ZSH_BLOCK = """# >>> ai-tmux-workspace >>>
[ -f "$HOME/.local/share/ai-tmux-workspace/completions/workspace.zsh" ] && . "$HOME/.local/share/ai-tmux-workspace/completions/workspace.zsh"
# <<< ai-tmux-workspace >>>
"""

def completion_text(template):
    """Fill the completion template with the translated command descriptions."""
    names = ("open", "install", "install-hooks", "restart", "reload", "fit", "stop")
    commands = "\n".join(
        f"    '{name}:{t('installer.completion.cmd.' + name.replace('-', '_'))}'" for name in names)
    return template.replace("__COMMANDS__", commands)


BASH_BLOCK = """# >>> ai-tmux-workspace >>>
[ -f "$HOME/.local/share/ai-tmux-workspace/completions/workspace.bash" ] && . "$HOME/.local/share/ai-tmux-workspace/completions/workspace.bash"
# <<< ai-tmux-workspace >>>
"""


class Aborted(Exception):
    """The person declined, or there was no answer: stop without doing that step."""


class Skip(Exception):
    """A removal entry cannot be restored safely; leave it alone."""

    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


class ManifestError(Exception):
    pass


# --- paths -------------------------------------------------------------------------------------

def config_path():
    return paths.config_path()


def legacy_config_path():
    return paths.legacy_config_path()


def read_config_file(path):
    """Mapping of an existing config.yaml; raises on unreadable/invalid text so nothing is overwritten."""
    data = yaml_min.loads(Path(path).read_text(encoding="utf-8"))
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ValueError("config root is not a mapping")
    return data


def example_config():
    """The example config without `profiles`: the installer seeds only the marked providers."""
    data = read_config_file(ROOT / "config" / "config.example.yaml")
    data.pop("profiles", None)
    return data


def is_legacy_claude_deepseek(profile):
    """True only for the old Claude+DeepSeek profile: the Claude harness pointed at DeepSeek."""
    if not isinstance(profile, dict) or profile.get("command") != "claude":
        return False
    settings = profile.get("settings")
    if not isinstance(settings, dict):
        return False
    env = settings.get("env")
    base_url = env.get("ANTHROPIC_BASE_URL") if isinstance(env, dict) else None
    if not isinstance(base_url, str):
        base_url = settings.get("base_url")
    return isinstance(base_url, str) and LEGACY_DEEPSEEK_HOST in base_url.lower()


def migrate_legacy_profiles(profiles, agent):
    """Apply the known legacy renames/removals to an existing config, in place.

    A rename happens only when the canonical key is absent, so a config that already has both keeps
    both. `deepseek` is removed only when it is the old Claude+DeepSeek profile; a homonym from
    another harness is preserved. Returns the applied old->new map and the matching `agent`.
    """
    applied = {}
    if not isinstance(profiles, dict):
        return applied, agent
    for old, new in LEGACY_PROFILE_RENAMES.items():
        if old in profiles and new not in profiles:
            profiles[new] = profiles.pop(old)
            applied[old] = new
    if LEGACY_DEEPSEEK in profiles and is_legacy_claude_deepseek(profiles[LEGACY_DEEPSEEK]):
        del profiles[LEGACY_DEEPSEEK]
        applied[LEGACY_DEEPSEEK] = LEGACY_DEEPSEEK_TARGET
    if isinstance(agent, str) and agent in applied:
        agent = applied[agent]
    return applied, agent


def state_dir():
    override = os.environ.get("AT_WS_INSTALL_STATE")
    if override:
        return Path(override).expanduser()
    return paths.state_dir() / "installer"


def generated_settings_dir():
    """The workspace-generated per-profile settings: WS_STATE/settings, exactly as lib.py uses."""
    return paths.settings_dir()


def completion_dir():
    return layout_root() / "completions"


def layout_root():
    """Root of the installed layout (`app/`, `harness/`, `security/`).

    `install.sh` historically points AT_WS_INSTALL_DIR at the clone (`.../app`), while the layout
    root is its parent: the harness and the mechanism are siblings of the clone, never inside it.
    """
    root = paths.install_dir()
    return root.parent if root.name == "app" else root


def harness_candidates():
    override = os.environ.get("AT_HARNESS_SOURCE")
    if override:
        return [Path(override).expanduser()]
    return [
        HOME / "Codes" / "ambiente-de-trabalho" / "harness",
        HOME / "ambiente-de-trabalho" / "harness",
        ROOT.parent / "ambiente-de-trabalho" / "harness",
    ]


def harness_source():
    """Directory with forcado.yaml, from the first candidate that has it."""
    for candidate in harness_candidates():
        for path in (candidate, candidate / "harness"):
            if (path / "forcado.yaml").is_file():
                return path
    return None


def clone_target(candidate):
    candidate = Path(candidate)
    return candidate.parent if candidate.name == "harness" else candidate


def display(path):
    text = str(path)
    home = str(HOME)
    if text == home:
        return "~"
    if text.startswith(home + os.sep):
        return "~" + text[len(home):]
    return text


def within(path, parent):
    try:
        Path(os.path.abspath(str(path))).relative_to(os.path.abspath(str(parent)))
        return True
    except ValueError:
        return False


def remove_path(path):
    path = Path(path)
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.is_dir():
        shutil.rmtree(path)


def file_for_edit(path):
    """A real file to append to. A symlink is followed only when the target is inside HOME."""
    path = Path(path)
    if path.is_symlink():
        resolved = path.resolve()
        return resolved if within(resolved, HOME) else None
    return path


def is_root():
    if os.geteuid() == 0:
        return True
    return any(os.environ.get(name) for name in ("SUDO_USER", "SUDO_UID", "PKEXEC_UID", "DOAS_USER"))


def package_manager():
    for name in ("pacman", "apt-get", "dnf", "zypper", "brew"):
        if shutil.which(name):
            return name
    return None


def platform_name():
    """sys.platform, overridable with AT_WS_PLATFORM (tests only) to exercise the macOS hints."""
    return os.environ.get("AT_WS_PLATFORM") or sys.platform


def install_command(name, manager):
    """Command shown to the person. Never executed by the assistant."""
    if name == "claude":
        return CLAUDE_INSTALLER
    if manager is None:
        return None
    package = PACKAGE_NAMES.get(manager, {}).get(name, name)
    if manager == "brew":
        return f"brew install {package}"
    flags = "-S --needed" if manager == "pacman" else "install -y"
    return f"pkexec {manager} {flags} {package}"


# --- privileged commands -----------------------------------------------------------------------

PRIVILEGED_NOT_RUN = 125    # shell convention: the command was not executed


def run_privileged(argv):
    """Entry point for a privileged command (a system step or `workspace privileged`).

    tmux.privileged runs it on a separate tmux screen and returns its exit code. Without an
    attached client nothing runs: the command is printed and the person confirms; this returns
    PRIVILEGED_NOT_RUN so a caller never mistakes it for success.
    """
    if not argv:
        UI().error(t("privileged.usage"))
        return 2
    code = tmux.privileged(argv)
    return PRIVILEGED_NOT_RUN if code is None else code


# --- text UI -----------------------------------------------------------------------------------

class UI:
    def __init__(self, stdin=None, stdout=None, stderr=None):
        self.inp = stdin or sys.stdin
        self.out = stdout or sys.stdout
        self.err = stderr or sys.stderr
        self.color = bool(getattr(self.out, "isatty", lambda: False)()) and not os.environ.get("NO_COLOR")

    def _style(self, text, code):
        return f"{code}{text}{colors.R}" if self.color else text

    def line(self, text=""):
        self.out.write(text + "\n")
        self.out.flush()

    def section(self, text):
        self.line()
        self.line(self._style(f"=== {text} ===", colors.B + colors.BLUE))

    def ok(self, text):
        self.line(self._style(t("installer.status.ok"), colors.GREEN) + text)

    def warn(self, text):
        self.line(self._style(t("installer.status.warn"), colors.YELLOW) + text)

    def error(self, text):
        self.err.write(self._style(t("installer.status.error"), colors.RED) + text + "\n")
        self.err.flush()

    def _answer(self):
        """One line from stdin; EOF aborts the step (nothing is done without an answer)."""
        self.out.flush()
        answer = self.inp.readline()
        if answer == "":
            self.line(t("installer.abort.no_answer"))
            raise Aborted()
        return answer.strip()

    def confirm(self, key, **kw):
        """Explicit confirmation; EOF and any answer not listed as yes abort the step."""
        self.out.write(t(key, **kw) + t("installer.answer.hint"))
        if self._answer().lower() not in t("installer.answer.yes").split():
            self.line(t("installer.abort.declined"))
            raise Aborted()

    def yes_no(self, key, **kw):
        """A yes/no choice where "no" is a valid answer, not an abort (item 93 management)."""
        self.out.write(t(key, **kw) + t("installer.answer.hint"))
        return self._answer().lower() in t("installer.answer.yes").split()

    def providers(self, key):
        """Mark provider names from PROVIDERS, by number or exact name; empty means none.

        The answer is returned in canonical order, so the first marked profile is stable."""
        options = "\n".join(f"  {index}. {name}" for index, name in enumerate(PROVIDERS, 1))
        self.out.write(t(key, options=options) + t("installer.answer.text_hint"))
        chosen = set()
        for token in re.split(r"[\s,]+", self._answer().lower()):
            if not token:
                continue
            if token.isdigit() and 1 <= int(token) <= len(PROVIDERS):
                chosen.add(PROVIDERS[int(token) - 1])
            elif token in PROVIDERS:
                chosen.add(token)
            else:
                self.error(t("installer.providers.invalid", option=token, count=len(PROVIDERS)))
                raise Aborted()
        return [name for name in PROVIDERS if name in chosen]

    def default_profile(self, key, names):
        """Choose the default profile among the marked ones; an empty answer keeps the first."""
        options = "\n".join(f"  {index}. {name}" for index, name in enumerate(names, 1))
        self.out.write(t(key, options=options, default=names[0]) + t("installer.answer.text_hint"))
        answer = self._answer()
        if not answer:
            return names[0]
        token = answer.split()[0].lower()
        if token.isdigit() and 1 <= int(token) <= len(names):
            return names[int(token) - 1]
        if token in names:
            return token
        self.error(t("installer.default.invalid", option=token, count=len(names)))
        raise Aborted()


# --- manifest and backup -----------------------------------------------------------------------

class Manifest:
    """What install touched, in the order it was touched, with a backup of the previous state."""

    def __init__(self, directory, entries=None, state_dirs=None):
        self.directory = Path(directory)
        self.path = self.directory / "manifest.json"
        self.backup_dir = self.directory / "backup"
        self.entries = entries or []
        self.state_dirs_created = state_dirs or []
        self._by_path = {entry["path"]: entry for entry in self.entries}

    @classmethod
    def create(cls, directory):
        directory = Path(directory)
        if (directory / "manifest.json").exists():
            existing = cls.load(directory)
            if existing is None:
                raise ManifestError(str(directory / "manifest.json"))
            return existing
        created, probe = [], directory
        while not probe.exists():
            created.append(str(probe))
            if probe.parent == probe:
                break
            probe = probe.parent
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "backup").mkdir(exist_ok=True)
        manifest = cls(directory, state_dirs=created)
        manifest.save()
        return manifest

    @classmethod
    def load(cls, directory):
        path = Path(directory) / "manifest.json"
        if not path.is_file():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as error:
            raise ManifestError(str(error))
        if not isinstance(data, dict) or data.get("version") != MANIFEST_VERSION:
            raise ManifestError("unsupported manifest")
        entries = data.get("entries")
        if not isinstance(entries, list):
            raise ManifestError("invalid entries")
        return cls(directory, entries, data.get("state_dirs_created") or [])

    def save(self):
        self.directory.mkdir(parents=True, exist_ok=True)
        data = {"version": MANIFEST_VERSION, "entries": self.entries,
                "state_dirs_created": self.state_dirs_created}
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        os.replace(tmp, self.path)

    def _next_backup(self):
        self.backup_dir.mkdir(parents=True, exist_ok=True)
        index = 0
        while (self.backup_dir / str(index)).exists():
            index += 1
        return self.backup_dir / str(index)

    def record(self, path, tree=False):
        """Record the state before the first touch of this path; later touches keep the original."""
        key = str(Path(path))
        if key in self._by_path:
            return
        entry = {"path": key}
        target = Path(path)
        if target.is_symlink():
            entry.update(kind="symlink", target=os.readlink(target))
        elif target.is_dir():
            backup = self._next_backup()
            shutil.copytree(target, backup, symlinks=True)
            entry.update(kind="dir", backup=str(backup))
        elif target.exists():
            backup = self._next_backup()
            shutil.copy2(target, backup)
            entry.update(kind="file", backup=str(backup))
        else:
            entry["kind"] = "absent_tree" if tree else "absent"
        self.entries.append(entry)
        self._by_path[key] = entry
        self.save()

    def record_clone(self, path, url):
        key = str(Path(path))
        if key in self._by_path:
            return
        entry = {"path": key, "kind": "clone", "url": url}
        self.entries.append(entry)
        self._by_path[key] = entry
        self.save()

    def drop(self, path):
        key = str(Path(path))
        entry = self._by_path.pop(key, None)
        if entry is None:
            return
        self.entries.remove(entry)
        self.save()

    def cleanup(self):
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass
        shutil.rmtree(self.backup_dir, ignore_errors=True)
        for directory in self.state_dirs_created:      # deepest first
            try:
                Path(directory).rmdir()
            except OSError:
                pass
        try:
            self.directory.rmdir()
        except OSError:
            pass


def restore_clone(path, url):
    path = Path(path)
    if path.is_symlink() or not path.exists():
        return
    if not path.is_dir():
        raise Skip("not_empty")
    git = shutil.which("git")
    if not git:
        raise Skip("clone_dirty")
    origin = subprocess.run([git, "-C", str(path), "remote", "get-url", "origin"],
                            capture_output=True, text=True)
    if origin.returncode != 0 or url not in origin.stdout:
        raise Skip("clone_url")
    status = subprocess.run([git, "-C", str(path), "status", "--porcelain"],
                            capture_output=True, text=True)
    if status.returncode != 0 or status.stdout.strip():
        raise Skip("clone_dirty")
    shutil.rmtree(path)


def restore_entry(entry):
    path = Path(entry["path"])
    kind = entry["kind"]
    if kind == "symlink":
        if path.is_dir() and not path.is_symlink():
            raise Skip("not_empty")
        remove_path(path)
        os.symlink(entry["target"], path)
    elif kind == "file":
        if path.is_dir() and not path.is_symlink():
            raise Skip("not_empty")
        remove_path(path)
        shutil.copy2(entry["backup"], path)
    elif kind == "dir":
        remove_path(path)
        shutil.copytree(entry["backup"], path, symlinks=True)
    elif kind == "absent":
        if path.is_dir() and not path.is_symlink():
            try:
                path.rmdir()
            except OSError:
                raise Skip("not_empty")
        elif path.is_symlink() or path.exists():
            path.unlink()
    elif kind == "absent_tree":
        if path.is_symlink() or path.exists():
            remove_path(path)
    elif kind == "clone":
        restore_clone(path, entry.get("url") or CLONE_URL)
    else:
        raise ValueError(f"unknown manifest kind: {kind}")


def entry_allowed(entry):
    """Only restore inside HOME or the configured config path; the manifest is not an order."""
    path = os.path.abspath(entry["path"])
    config = os.path.abspath(str(config_path()))
    if within(path, HOME) or path == config:
        return True
    # A parent directory created only for an AT_WS_CONFIG outside HOME: restore is rmdir, never recursive.
    return entry["kind"] == "absent" and within(config, path)


# --- installer ---------------------------------------------------------------------------------

class Assistant:
    def __init__(self, ui):
        self.ui = ui
        self.manifest = None
        self.manage = True      # `manage: false` only when the person says the workspace does not manage
        self.harness_managed = True     # separate switch: `manage: true` always manages the harness
        self.dependency_warnings = []   # labels repeated by finish() so success is never silent

    def manifest_for(self):
        if self.manifest is None:
            self.manifest = Manifest.create(state_dir())
        return self.manifest

    def ensure_dir(self, path):
        path = Path(path)
        missing, probe = [], path
        while not probe.exists() and not probe.is_symlink():
            missing.append(probe)
            if probe.parent == probe:
                break
            probe = probe.parent
        for directory in reversed(missing):
            self.manifest_for().record(directory)
            directory.mkdir(exist_ok=True)

    def write(self, path, content, mode=0o644):
        path = Path(path)
        self.ensure_dir(path.parent)
        self.manifest_for().record(path)
        if path.is_dir() and not path.is_symlink():
            raise OSError(f"{path} is a directory")
        remove_path(path)
        path.write_text(content, encoding="utf-8")
        os.chmod(path, mode)

    def append_block(self, path, block, marker=">>> ai-tmux-workspace >>>"):
        path = Path(path)
        self.ensure_dir(path.parent)
        existing = path.read_text(encoding="utf-8") if path.exists() else ""
        if marker in existing:
            return
        self.manifest_for().record(path)
        prefix = "" if not existing or existing.endswith("\n") else "\n"
        path.write_text(existing + prefix + block + "\n", encoding="utf-8")

    def install(self):
        if is_root():
            self.ui.error(t("installer.root.refused"))
            return 1
        self.ui.section(t("installer.title"))
        self.ui.line(t("installer.install.intro", bin=display(HOME / CLI_DIR / "workspace"),
                       harness=display(layout_root() / "harness")))
        try:
            self.ui.confirm("installer.install.confirm")
            self.prepare_layout()
            self.dependencies()
            self.install_cli()
            self.install_completion()
            self.install_config()
            self.install_harness()
            self.install_hooks()
        except Aborted:
            if self.manifest is not None and self.manifest.entries:
                self.ui.line(t("installer.abort.partial"))
            return 1
        except (ManifestError, OSError) as error:
            self.ui.error(t("installer.error", error=str(error)))
            return 1
        self.finish()
        return 0

    def prepare_layout(self):
        """Create the control plane, state and conference folders and migrate the old state.

        The folders are not manifest entries: `remove` only removes them when they end up empty, so
        migrated state (the person's data) is never deleted by the killswitch. The installed copy
        (`app/`, `harness/`, `security/`) is not touched here; `install_harness` owns it.
        """
        planned = (paths.conference_dir(), paths.state_dir(), paths.config_dir())
        created = set()
        for directory in planned:
            probe = directory
            while not probe.exists() and not probe.is_symlink():
                created.add(str(probe))
                if probe.parent == probe:
                    break
                probe = probe.parent
        try:
            paths.ensure_layout(migrate=False)
            moved = paths.migrate_state()
        except OSError as error:
            self.ui.warn(t("installer.migrate.failed", error=str(error)))
            moved = []
        manifest = self.manifest_for()
        # Deepest first, so `remove` can rmdir from the leaves up. Directories that end up with the
        # person's migrated state stay where they are (rmdir fails silently in cleanup).
        for directory in sorted(created, key=lambda name: len(Path(name).parts), reverse=True):
            if directory not in manifest.state_dirs_created:
                manifest.state_dirs_created.append(directory)
        manifest.save()
        if moved:
            self.ui.line(t("installer.migrate.moved", names=", ".join(moved),
                           path=display(paths.state_dir())))

    def dependency_status(self):
        """(found labels, unmet (name, label) pairs) with the version probes the tests fake."""
        found, unmet = [], []
        for name in REQUIRED:
            path = shutil.which(name)
            if not path:
                unmet.append((name, name))
                continue
            version = tool_version(name, path)
            minimum = MIN_VERSIONS.get(name)
            if minimum and version and version < minimum:
                # Language-neutral label; the surrounding text comes from i18n.
                unmet.append((name, f"{name} {format_version(version)} < {format_version(minimum)}"))
            else:
                found.append(f"{name} {format_version(version)}" if version else name)
        bash_version = tool_version("bash", BASH)
        minimum = MIN_VERSIONS["bash"]
        if bash_version and bash_version < minimum:
            unmet.append(("bash", f"bash {format_version(bash_version)} < {format_version(minimum)}"))
        else:
            found.append(f"bash {format_version(bash_version)}" if bash_version else f"bash ({BASH})")
        return found, unmet

    def dependencies(self):
        self.ui.section(t("installer.deps.title"))
        found, unmet = self.dependency_status()
        self.dependency_warnings = [label for _name, label in unmet]
        optional_missing = [name for name in OPTIONAL if not shutil.which(name)]
        if found:
            self.ui.line(t("installer.deps.found", names=", ".join(found)))
        manager = package_manager()
        if unmet:
            self.ui.warn(t("installer.deps.missing_required",
                           names=", ".join(label for _name, label in unmet)))
            # A fresh Mac has no package manager: show Homebrew first, never sudo.
            if platform_name() == "darwin" and manager is None:
                self.ui.line(t("installer.deps.command", name="Homebrew",
                               command=HOMEBREW_BOOTSTRAP))
                self.ui.line(t("installer.deps.command", name="bash tmux python",
                               command="brew install bash tmux python"))
            for name, _label in unmet:
                command = install_command(name, manager)
                if command:
                    self.ui.line(t("installer.deps.command", name=name, command=command))
                else:
                    self.ui.line(t("installer.deps.unknown_manager", name=name))
            self.ui.line(t("installer.deps.no_privilege"))
            self.ui.confirm("installer.deps.confirm")
        if optional_missing:
            self.ui.line(t("installer.deps.optional", names=", ".join(optional_missing)))
            for name in optional_missing:
                command = install_command(name, manager)
                if command:
                    self.ui.line(t("installer.deps.optional_command", name=name, command=command))

    def install_cli(self):
        self.ui.section(t("installer.cli.title"))
        link = HOME / CLI_DIR / "workspace"
        target = ROOT / "bin" / "workspace"
        self.ui.line(t("installer.cli.plan", link=display(link), target=str(target)))
        self.ui.confirm("installer.cli.confirm")
        self.ensure_dir(link.parent)
        self.manifest_for().record(link)
        if link.is_dir() and not link.is_symlink():
            self.ui.error(t("installer.error", error=f"{display(link)} is a directory"))
            raise Aborted()
        remove_path(link)
        os.symlink(str(target), str(link))
        self.ui.ok(t("installer.cli.ok", link=display(link), target=str(target)))
        if str(link.parent) not in os.environ.get("PATH", "").split(os.pathsep):
            self.ui.warn(t("installer.cli.path_warning", dir=display(link.parent)))

    def install_completion(self):
        self.ui.section(t("installer.completion.title"))
        target_dir = completion_dir()
        files = {
            "zsh": (target_dir / "workspace.zsh", ZSH_COMPLETION, ".zshrc", ZSH_BLOCK),
            "bash": (target_dir / "workspace.bash", BASH_COMPLETION, ".bashrc", BASH_BLOCK),
        }
        shell = Path(os.environ.get("SHELL", "")).name
        if shell in files:
            installed, _content, rc_name, _block = files[shell]
            self.ui.line(t("installer.completion.plan", shell=shell,
                           file=display(installed), rc=display(HOME / rc_name)))
        else:
            self.ui.line(t("installer.completion.plan_unsupported", shell=shell or "?",
                           dir=display(target_dir)))
        self.ui.confirm("installer.completion.confirm")
        for installed, content, _rc, _block in files.values():
            if content is ZSH_COMPLETION:
                content = completion_text(content)
            self.write(installed, content)
        if shell in files:
            installed, _content, rc_name, block = files[shell]
            rc = HOME / rc_name
            target = file_for_edit(rc)
            if target is None:
                self.ui.warn(t("installer.completion.symlink_outside", file=display(rc)))
            else:
                self.append_block(target, block)
                self.ui.ok(t("installer.completion.ok", shell=shell,
                             file=display(installed), rc=display(rc)))
        else:
            self.ui.warn(t("installer.completion.unsupported", shell=shell or "?",
                           dir=display(target_dir)))

    def install_config(self):
        """Ask the item 93/107 questions and seed/update only what was chosen.

        Before writing anything: whether the workspace manages the agent options (`manage`), which
        providers to include, and which marked profile is the default. An existing config keeps
        every other key and every profile it already had; the known legacy names are migrated and
        the marked profiles are added or updated. The install never seeds the whole example, so
        unmarked providers are not written."""
        self.ui.section(t("installer.config.title"))
        path = config_path()
        legacy = legacy_config_path()
        existing = path.exists() or path.is_symlink()
        if not existing and (legacy.exists() or legacy.is_symlink()):
            self.ui.warn(t("installer.config.legacy", legacy=display(legacy), config=display(path)))
            return
        self.manage = self.ui.yes_no("installer.manage.question")
        if self.manage:
            self.harness_managed = True
        else:
            self.harness_managed = self.ui.yes_no("installer.harness_managed.question")
        selected = self.ui.providers("installer.providers.question")
        default = (self.ui.default_profile("installer.default.question", selected)
                   if selected else None)
        if existing:
            try:
                data = read_config_file(path)
            except (OSError, ValueError) as error:
                self.ui.error(t("installer.config.read_failed", path=display(path), error=str(error)))
                raise Aborted()
            self.ui.line(t("installer.config.exists", path=display(path)))
        else:
            self.ui.line(t("installer.config.plan", path=display(path)))
            data = example_config()
        profiles = data.get("profiles")
        if not isinstance(profiles, dict):
            profiles = {}
        _applied, agent = migrate_legacy_profiles(profiles, data.get("agent"))
        for name in selected:
            profiles[name] = copy.deepcopy(PROVIDER_PROFILES[name])
        if default:
            agent = default
        if not agent:
            agent = "claude"
        data["manage"] = self.manage
        data["harness_managed"] = self.harness_managed
        data["agent"] = agent
        if profiles or "profiles" in data:
            data["profiles"] = profiles
        manage_text = t("installer.manage.true") if self.manage else t("installer.manage.false")
        selected_text = ", ".join(selected) if selected else t("installer.providers.none")
        self.ui.line(t("installer.config.update_plan", path=display(path), manage=manage_text,
                       agent=agent, profiles=selected_text))
        self.ui.confirm("installer.config.update_confirm" if existing else "installer.config.confirm")
        text = yaml_min.dumps(data)
        if yaml_min.loads(text) != data:
            raise OSError(t("installer.config.roundtrip", path=display(path)))
        self.ensure_dir(path.parent)
        self.write(path, text)
        self.ui.ok(t("installer.config.updated" if existing else "installer.config.created",
                     path=display(path)))

    def install_harness(self):
        if not self.harness_managed:
            self.ui.line(t("installer.harness_managed.skipped"))
            return
        self.ui.section(t("installer.harness.title"))
        source = harness_source()
        if source is None:
            candidate = harness_candidates()[0]
            self.ui.warn(t("installer.harness.missing", path=display(candidate)))
            target = clone_target(candidate)
            if target.exists() or target.is_symlink():
                self.ui.error(t("installer.harness.clone_exists", path=display(target)))
                raise Aborted()
            command = shlex.join(["git", "clone", "--depth", "1", CLONE_URL, str(target)])
            self.ui.line(t("installer.harness.clone", command=command))
            self.ui.confirm("installer.harness.clone_confirm")
            self.ensure_dir(target.parent)
            self.manifest_for().record_clone(target, CLONE_URL)
            result = subprocess.run(["git", "clone", "--depth", "1", CLONE_URL, str(target)],
                                    check=False)
            if result.returncode != 0:
                remove_path(target)
                self.manifest_for().drop(target)
                self.ui.error(t("installer.harness.clone_failed", code=result.returncode))
                raise Aborted()
            source = harness_source()
            if source is None:
                self.ui.error(t("installer.harness.missing", path=display(candidate)))
                raise Aborted()
        self.ui.line(t("installer.harness.source", path=display(source)))
        files = harness_files(source) + [f"security/{name}" for name in security_files(source)]
        self.ui.line(t("installer.harness.files", files=", ".join(files)))
        lean = ROOT / "harness" / "HARNESS.md"
        entry = lean if lean.is_file() else source / "HARNESS.md"
        if not entry.is_file():
            entry = source / "CLAUDE.md"
        if entry.is_file():
            text = entry.read_text(encoding="utf-8", errors="replace")
            lines = text.splitlines()
            if len(lines) > 60:
                text = "\n".join(lines[:60]) + "\n…"
            self.ui.line(t("installer.harness.content", file=entry.name, text=text))
        self.ui.confirm("installer.harness.confirm_want")
        self.ui.confirm("installer.harness.confirm_agree")
        target = layout_root() / "harness"
        security = layout_root() / "security"
        self.ensure_dir(target.parent)
        self.manifest_for().record(target, tree=True)
        self.manifest_for().record(security, tree=True)
        try:
            warning = copy_harness(source, target)
        except OSError as error:
            self.ui.error(t("installer.harness.copy_failed", error=str(error)))
            raise Aborted()
        if warning:
            self.ui.warn(t("installer.harness.render_failed",
                           path=display(security / "settings.json"), error=warning))
        self.ui.ok(t("installer.harness.installed", path=display(target)))
        self.ui.ok(t("installer.security.installed", path=display(security)))
        if not self.manage:
            self.install_harness_shim()

    def install_harness_shim(self):
        """Without the SessionStart hook, ~/.claude/CLAUDE.md imports the installed HARNESS.md (`@path`,
        one line). Created or updated in place between the markers; the person's text is kept."""
        path = HOME / ".claude" / "CLAUDE.md"
        block = shim_block("@" + display(layout_root() / "harness" / "HARNESS.md"))
        existing = path.read_text(encoding="utf-8") if path.exists() else ""
        try:
            merged = merge_shim(existing, block)
        except ShimError as error:
            # Provisional pt-BR text: the key for this message lives in i18n/pt-BR.json (not in scope).
            self.ui.warn(f"Bloco do harness em {display(path)} está quebrado ({error}); não mexi no arquivo.")
            return
        if merged != existing:
            self.ensure_dir(path.parent)
            self.manifest_for().record(path)
            path.write_text(merged, encoding="utf-8")
        self.ui.ok(t("installer.harness.shim_ok", path=display(path)))

    def install_hooks(self):
        """Register the workspace hooks/statusLine in the person's Claude Code settings.

        The registered command itself patches the file, so the whole file (and the backup it
        creates) is recorded before the call: removal restores both verbatim, never editing
        third-party hooks selectively.
        """
        self.ui.section(t("installer.hooks.title"))
        if not self.manage:
            self.ui.line(t("installer.hooks.skipped"))
            return
        claude = HOME / ".claude"
        settings = claude / "settings.json"
        backup = claude / "settings.json.bak-workspace"
        self.ui.line(t("installer.hooks.plan", settings=display(settings), backup=display(backup)))
        self.ui.confirm("installer.hooks.confirm")
        self.ensure_dir(claude)
        self.manifest_for().record(settings)
        self.manifest_for().record(backup)
        result = subprocess.run([BASH, str(ROOT / "bin" / "workspace"), "install-hooks"],
                                capture_output=True, text=True)
        output = (result.stdout or "").strip()
        if result.returncode != 0:
            detail = (result.stderr or output or "").strip()
            self.ui.error(t("installer.hooks.failed", code=result.returncode, output=detail))
            raise Aborted()
        if output:
            self.ui.line(output)
        self.ui.ok(t("installer.hooks.done", path=display(settings)))

    def finish(self):
        warnings = list(self.dependency_warnings)
        for name in REQUIRED:
            if not shutil.which(name) and name not in warnings:
                warnings.append(name)
        if warnings:
            self.ui.warn(t("installer.done.missing", names=", ".join(warnings)))
        self.ui.section(t("installer.done.title"))
        self.ui.line(t("installer.done.next") if self.manage else t("installer.done.next_unmanaged"))

    def remove(self):
        if is_root():
            self.ui.error(t("installer.root.refused"))
            return 1
        self.ui.section(t("installer.remove.title"))
        try:
            manifest = Manifest.load(state_dir())
        except ManifestError as error:
            self.ui.error(t("installer.error", error=str(error)))
            return 1
        settings = generated_settings_dir()
        has_settings = settings.exists() or settings.is_symlink()
        if manifest is None or not manifest.entries:
            if not has_settings:
                self.ui.line(t("installer.remove.nothing"))
                if manifest is not None:
                    manifest.cleanup()
                elif state_dir().exists():
                    try:
                        state_dir().rmdir()
                    except OSError:
                        pass
                return 0
            self.ui.line(t("installer.remove.settings_only", path=display(settings)))
            try:
                self.ui.confirm("installer.remove.confirm")
            except Aborted:
                return 1
            code = self.remove_generated_settings(settings)
            if code == 0 and manifest is not None:
                manifest.cleanup()
            return code
        self.ui.line(t("installer.remove.intro"))
        for entry in manifest.entries:
            self.ui.line(t("installer.remove.item", path=display(entry["path"]),
                           kind=t("installer.remove.kind." + entry["kind"])))
        if has_settings:
            self.ui.line(t("installer.remove.item", path=display(settings),
                           kind=t("installer.remove.kind.settings")))
        try:
            self.ui.confirm("installer.remove.confirm")
        except Aborted:
            return 1
        failed = False
        for entry in reversed(manifest.entries):
            if not entry_allowed(entry):
                self.ui.warn(t("installer.remove.entry_skipped", path=display(entry["path"]),
                               reason=t("installer.remove.reason.outside")))
                failed = True
                continue
            try:
                restore_entry(entry)
            except Skip as error:
                self.ui.warn(t("installer.remove.entry_skipped", path=display(entry["path"]),
                               reason=t("installer.remove.reason." + error.reason)))
                continue
            except Exception as error:                     # noqa: BLE001 - report and keep the manifest
                failed = True
                self.ui.error(t("installer.remove.error", path=display(entry["path"]),
                                error=str(error)))
                continue
            self.ui.ok(t("installer.remove.entry_ok", path=display(entry["path"])))
        if has_settings:
            try:
                remove_path(settings)
                self.ui.ok(t("installer.remove.entry_ok", path=display(settings)))
            except Exception as error:                     # noqa: BLE001 - report and keep the manifest
                failed = True
                self.ui.error(t("installer.remove.error", path=display(settings),
                                error=str(error)))
        if failed:
            self.ui.error(t("installer.remove.partial"))
            return 1
        manifest.cleanup()
        self.ui.ok(t("installer.remove.done"))
        return 0

    def remove_generated_settings(self, path):
        try:
            remove_path(path)
        except Exception as error:                         # noqa: BLE001 - report and keep state
            self.ui.error(t("installer.remove.error", path=display(path), error=str(error)))
            return 1
        self.ui.ok(t("installer.remove.entry_ok", path=display(path)))
        self.ui.ok(t("installer.remove.settings_done"))
        return 0


def install_policy_files(target):
    """Copy the gate, the policy engine, its config and the OpenCode adapter into an installed `security/`.

    Overwrites in place, so running it again leaves byte-identical files (idempotent). Returns the
    names that were actually present in this checkout, in declaration order.
    """
    target = Path(target)
    installed = []
    for folder, name, destination in POLICY_SOURCES:
        source = ROOT / folder / name
        if not source.is_file():
            continue
        target.mkdir(parents=True, exist_ok=True)
        (target / destination).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target / destination)
        installed.append(destination)
    return installed


def sha256_file(path):
    """sha256 of a file, or None when it does not exist (the record keeps it explicit)."""
    path = Path(path)
    if not path.is_file():
        return None
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def git_commit(path):
    """HEAD of the git worktree containing path, or None when the source is not a git repo."""
    git = shutil.which("git")
    if not git:
        return None
    try:
        result = subprocess.run([git, "-C", str(path), "rev-parse", "HEAD"],
                                capture_output=True, text=True)
    except OSError:
        return None
    return (result.stdout.strip() or None) if result.returncode == 0 else None


def source_record(source, target):
    """The lateral source.json: canonical source, its commit and both HARNESS.md hashes."""
    return {
        "source": str(Path(source).resolve()),
        "commit": git_commit(source),
        "source_sha256": sha256_file(Path(source) / "HARNESS.md"),
        "installed_sha256": sha256_file(Path(target) / "HARNESS.md"),
    }


def harness_files(source):
    """Text files of the canonical harness: only rule and declaration, never the mechanism."""
    source = Path(source)
    names = [name for name in ("HARNESS.md", "CLAUDE.md", "forcado.yaml", "ORIGEM.md")
             if (source / name).is_file()]
    rules = source / "regras"
    if rules.is_dir():
        names.extend(f"regras/{path.name}" for path in sorted(rules.glob("*.md"))
                     if path.is_file())
    return list(dict.fromkeys(names))


def security_files(source=None):
    """Mechanism artifacts copied into the installed `security/`, plus the rendered settings.json.

    `source` is accepted for symmetry with `harness_files`; the mechanism comes from this checkout,
    not from the canonical text source.
    """
    names = [destination for folder, name, destination in POLICY_SOURCES
             if (ROOT / folder / name).is_file()]
    names.append("settings.json")
    return list(dict.fromkeys(names))


# Claude shim: one import line between two markers the installation owns. Text outside the markers is
# the person's and is never rewritten; the block itself is replaced in place, so a second run is a no-op.
SHIM_START = ">>> ai-tmux-workspace harness >>>"
SHIM_END = "<<< ai-tmux-workspace harness <<<"


class ShimError(ValueError):
    """The managed block is damaged (repeated or unpaired markers): the file is left untouched."""


def shim_block(import_line):
    return "\n".join((SHIM_START, import_line, SHIM_END))


def merge_shim(existing, block):
    """`existing` with the managed block set to `block`: replaced where the block is, appended when
    there is none. Every byte outside the markers is kept as it was."""
    if SHIM_START not in existing and SHIM_END not in existing:
        prefix = "" if not existing or existing.endswith("\n") else "\n"
        return existing + prefix + block + "\n"
    if existing.count(SHIM_START) != 1 or existing.count(SHIM_END) != 1:
        raise ShimError("markers repeated or unpaired")
    start = existing.index(SHIM_START)
    end = existing.index(SHIM_END)
    if end < start:
        raise ShimError("end marker before start marker")
    line_start = existing.rfind("\n", 0, start) + 1
    line_end = existing.find("\n", end)
    if line_end == -1:
        line_end = len(existing)
    return existing[:line_start] + block + existing[line_end:]


def copy_harness(source, target):
    """Replace `target` with a copy of the canonical harness `source`, as the install does: the text
    (`HARNESS.md`, `forcado.yaml`, `regras/`, `source.json`) in `target`, and the mechanism (gate,
    policy files and the rendered `settings.json`) in `target.parent/"security"`. Returns the render
    error text when settings.json could not be rendered, else None. Raises OSError when the copy
    itself fails. Shared with `workspace config update`, which refreshes the installed copy."""
    source, target = Path(source), Path(target)
    security = target.parent / "security"
    lean = ROOT / "harness" / "HARNESS.md"
    remove_path(target)
    shutil.copytree(source, target, symlinks=True)
    if lean.is_file():
        shutil.copy2(lean, target / "HARNESS.md")
    remove_path(security)
    install_policy_files(security)
    rendered = subprocess.run(
        [sys.executable, str(security / "gate.py"), "render",
         "--harness", str(target), "--out", str(security / "settings.json")],
        capture_output=True, text=True)
    warning = None
    if rendered.returncode != 0:
        warning = (rendered.stderr or "").strip() or str(rendered.returncode)
    (target / SOURCE_FILE).write_text(
        json.dumps(source_record(source, target), indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    if (target / "HARNESS.md").is_file():
        os.chmod(target / "HARNESS.md", 0o444)
    return warning


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    ui = UI()
    if argv and argv[0] in ("-h", "--help"):
        ui.line(t("installer.usage"))
        return 0
    if argv and argv[0] == "--privileged":
        return run_privileged(argv[1:])
    assistant = Assistant(ui)
    if "--remove" in argv:
        return assistant.remove()
    unknown = [argument for argument in argv if not argument.startswith("-")]
    if unknown:
        ui.error(t("installer.error", error=t("installer.unknown_arg", arg=unknown[0])))
        return 2
    return assistant.install()


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
