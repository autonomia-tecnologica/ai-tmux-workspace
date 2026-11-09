#!/usr/bin/env python3
"""Interactive installer for ai-tmux-workspace, and its killswitch.

`install.sh` checks python3 and calls this assistant; `remove.sh` calls it with `--remove`.
Every step asks for explicit confirmation, every path it touches is recorded in a manifest
with a backup, and privileged commands are only shown: the system steps that ask for sudo
belong to backlog item 35, so this installer never runs sudo/pkexec or installs packages.

Stdlib only. All end-user text comes from i18n/pt-BR.json (`installer.*` keys).

Path overrides (also used by the tests): HOME, AT_WS_CONFIG, WS_ESTADO,
AT_HARNESS_SOURCE (disables the default harness search when set), AT_WS_INSTALL_STATE and SHELL.
"""
import hashlib
import json
import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

import colors
from i18n import t

ROOT = Path(__file__).resolve().parent.parent
HOME = Path(os.environ.get("HOME") or Path.home())
BASH = "/bin/bash" if os.path.exists("/bin/bash") else (shutil.which("bash") or "bash")
CLONE_URL = "https://github.com/autonomia-tecnologica/ambiente-de-trabalho.git"
HARNESS_DIR = Path(".local", "share", "ai-tmux-workspace", "harness")
SOURCE_FILE = "source.json"
COMPLETION_DIR = Path(".local", "share", "ai-tmux-workspace", "completions")
# Installed next to harness.py: the policy/config pair and the two adapters' shared engine. The
# source tree (ambiente-de-trabalho) is copied first; these come from this repository.
POLICY_SOURCES = (
    ("harness", "policy.yaml", "policy.yaml"),
    ("harness", "opencode-policy.js", "opencode-policy.js"),
    ("src", "policy.py", "policy.py"),
    ("src", "yaml_min.py", "yaml_min.py"),
    ("i18n", "pt-BR.json", "i18n/pt-BR.json"),
)
CLI_DIR = Path(".local", "bin")
REQUIRED = ("tmux", "python3", "git", "claude")
OPTIONAL = ("gh", "nvim")
MANIFEST_VERSION = 1

# Package names per manager; only shown to the person, never executed by the assistant.
PACKAGE_NAMES = {
    "pacman": {"tmux": "tmux", "git": "git", "python3": "python", "gh": "github-cli", "nvim": "neovim"},
    "apt-get": {"tmux": "tmux", "git": "git", "python3": "python3", "gh": "gh", "nvim": "neovim"},
    "dnf": {"tmux": "tmux", "git": "git", "python3": "python3", "gh": "gh", "nvim": "neovim"},
    "zypper": {"tmux": "tmux", "git": "git", "python3": "python3", "gh": "gh", "nvim": "neovim"},
    "brew": {"tmux": "tmux", "git": "git", "python3": "python3", "gh": "gh", "nvim": "neovim"},
}
CLAUDE_INSTALLER = "curl -fsSL https://claude.ai/install.sh | bash"

ZSH_COMPLETION = """#compdef workspace
# ai-tmux-workspace autocomplete (managed by install.sh)
_workspace_completion() {
  local -a commands
  commands=(
    'open:Abrir um repo'
    'install:Criar o link do comando workspace'
    'install-hooks:Registrar os hooks do Claude Code'
    'restart:Derrubar e subir o workspace'
    'reload:Recarregar config, tmux.conf e paineis'
    'fit:Reaplicar os tamanhos dos paineis'
    'stop:Parar o servidor do perfil'
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
    value = os.environ.get("AT_WS_CONFIG")
    return Path(value).expanduser() if value else HOME / ".config" / "ai-tmux-workspace" / "config.yaml"


def legacy_config_path():
    if "AT_WS_CONFIG" in os.environ:
        return config_path().with_suffix(".json")
    return HOME / ".config" / "ai-tmux-workspace" / "settings.json"


def state_dir():
    override = os.environ.get("AT_WS_INSTALL_STATE")
    if override:
        return Path(override).expanduser()
    base = os.environ.get("WS_ESTADO")
    root = Path(base).expanduser() if base else HOME / ".cache" / "workspace"
    return root / "installer"


def generated_settings_dir():
    """The workspace-generated per-profile settings: WS_ESTADO/settings, exactly as lib.py uses."""
    base = os.environ.get("WS_ESTADO")
    root = Path(base).expanduser() if base else HOME / ".cache" / "workspace"
    return root / "settings"


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
        self.line(self._style(f"=== {text} ===", colors.B + colors.AZUL))

    def ok(self, text):
        self.line(self._style("ok: ", colors.VERDE) + text)

    def warn(self, text):
        self.line(self._style("aviso: ", colors.AMARELO) + text)

    def error(self, text):
        self.err.write(self._style("erro: ", colors.VERMELHO) + text + "\n")
        self.err.flush()

    def confirm(self, key, **kw):
        """Explicit confirmation; EOF and anything but s/sim/y/yes abort the step."""
        self.out.write(t(key, **kw) + t("installer.answer.hint"))
        self.out.flush()
        answer = self.inp.readline()
        if answer == "":
            self.line(t("installer.abort.no_answer"))
            raise Aborted()
        if answer.strip().lower() not in ("s", "sim", "y", "yes"):
            self.line(t("installer.abort.declined"))
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

    def append_block(self, path, block):
        path = Path(path)
        self.ensure_dir(path.parent)
        existing = path.read_text(encoding="utf-8") if path.exists() else ""
        if ">>> ai-tmux-workspace >>>" in existing:
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
                       harness=display(HOME / HARNESS_DIR)))
        try:
            self.ui.confirm("installer.install.confirm")
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

    def dependencies(self):
        self.ui.section(t("installer.deps.title"))
        found = [name for name in REQUIRED if shutil.which(name)]
        missing = [name for name in REQUIRED if name not in found]
        optional_missing = [name for name in OPTIONAL if not shutil.which(name)]
        if found:
            self.ui.line(t("installer.deps.found", names=", ".join(found)))
        if missing:
            self.ui.warn(t("installer.deps.missing_required", names=", ".join(missing)))
            manager = package_manager()
            for name in missing:
                command = install_command(name, manager)
                if command:
                    self.ui.line(t("installer.deps.command", name=name, command=command))
                else:
                    self.ui.line(t("installer.deps.unknown_manager", name=name))
            self.ui.line(t("installer.deps.no_privilege"))
            self.ui.confirm("installer.deps.confirm")
        if optional_missing:
            self.ui.line(t("installer.deps.optional", names=", ".join(optional_missing)))
            manager = package_manager()
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
        files = {
            "zsh": (COMPLETION_DIR / "workspace.zsh", ZSH_COMPLETION, ".zshrc", ZSH_BLOCK),
            "bash": (COMPLETION_DIR / "workspace.bash", BASH_COMPLETION, ".bashrc", BASH_BLOCK),
        }
        shell = Path(os.environ.get("SHELL", "")).name
        if shell in files:
            relative, _content, rc_name, _block = files[shell]
            self.ui.line(t("installer.completion.plan", shell=shell,
                           file=display(HOME / relative), rc=display(HOME / rc_name)))
        else:
            self.ui.line(t("installer.completion.plan_unsupported", shell=shell or "?",
                           dir=display(HOME / COMPLETION_DIR)))
        self.ui.confirm("installer.completion.confirm")
        for relative, content, _rc, _block in files.values():
            self.write(HOME / relative, content)
        if shell in files:
            relative, _content, rc_name, block = files[shell]
            rc = HOME / rc_name
            target = file_for_edit(rc)
            if target is None:
                self.ui.warn(t("installer.completion.symlink_outside", file=display(rc)))
            else:
                self.append_block(target, block)
                self.ui.ok(t("installer.completion.ok", shell=shell,
                             file=display(HOME / relative), rc=display(rc)))
        else:
            self.ui.warn(t("installer.completion.unsupported", shell=shell or "?",
                           dir=display(HOME / COMPLETION_DIR)))

    def install_config(self):
        self.ui.section(t("installer.config.title"))
        path = config_path()
        legacy = legacy_config_path()
        if path.exists() or path.is_symlink():
            self.ui.line(t("installer.config.exists", path=display(path)))
            return
        if legacy.exists() or legacy.is_symlink():
            self.ui.warn(t("installer.config.legacy", legacy=display(legacy), config=display(path)))
            return
        self.ui.line(t("installer.config.plan", path=display(path)))
        self.ui.confirm("installer.config.confirm")
        self.ensure_dir(path.parent)
        self.manifest_for().record(path)
        shutil.copy2(ROOT / "config" / "config.example.yaml", path)
        self.ui.ok(t("installer.config.created", path=display(path)))

    def install_harness(self):
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
        self.ui.line(t("installer.harness.files", files=", ".join(harness_files(source))))
        entry = source / "HARNESS.md"
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
        target = HOME / HARNESS_DIR
        self.ensure_dir(target.parent)
        self.manifest_for().record(target, tree=True)
        remove_path(target)
        try:
            shutil.copytree(source, target, symlinks=True)
        except OSError as error:
            self.ui.error(t("installer.harness.copy_failed", error=str(error)))
            raise Aborted()
        shutil.copy2(ROOT / "harness" / "harness.py", target / "harness.py")
        install_policy_files(target)
        rendered = subprocess.run(
            [sys.executable, str(target / "harness.py"), "render",
             "--harness", str(target), "--out", str(target / "settings.json")],
            capture_output=True, text=True)
        if rendered.returncode != 0:
            self.ui.warn(t("installer.harness.render_failed",
                           path=display(target / "settings.json"),
                           error=(rendered.stderr or "").strip() or str(rendered.returncode)))
        self.write(target / SOURCE_FILE,
                   json.dumps(source_record(source, target), indent=2, ensure_ascii=False) + "\n")
        if (target / "HARNESS.md").is_file():
            os.chmod(target / "HARNESS.md", 0o444)
        self.ui.ok(t("installer.harness.installed", path=display(target)))

    def install_hooks(self):
        """Register the workspace hooks/statusLine in the person's Claude Code settings.

        The registered command itself patches the file, so the whole file (and the backup it
        creates) is recorded before the call: removal restores both verbatim, never editing
        third-party hooks selectively.
        """
        self.ui.section(t("installer.hooks.title"))
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
        missing = [name for name in REQUIRED if not shutil.which(name)]
        if missing:
            self.ui.warn(t("installer.done.missing", names=", ".join(missing)))
        self.ui.section(t("installer.done.title"))
        self.ui.line(t("installer.done.next"))

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
    """Copy the policy engine, its config and the OpenCode adapter into an installed harness.

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
    source = Path(source)
    names = [name for name in ("HARNESS.md", "CLAUDE.md", "forcado.yaml", "ORIGEM.md")
             if (source / name).is_file()]
    rules = source / "regras"
    if rules.is_dir():
        names.extend(f"regras/{path.name}" for path in sorted(rules.glob("*.md"))
                     if path.is_file())
    names.extend(destination for folder, name, destination in POLICY_SOURCES
                 if (ROOT / folder / name).is_file())
    return list(dict.fromkeys(names))


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    ui = UI()
    if argv and argv[0] in ("-h", "--help"):
        ui.line(t("installer.usage"))
        return 0
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
