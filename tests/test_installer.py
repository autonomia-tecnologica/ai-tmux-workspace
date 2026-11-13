"""Tests for the step-by-step installer (`install.sh` + `src/installer.py`) and its killswitch.

Every test uses a temporary HOME, a temporary state dir and a fake harness source, drives the
assistant with simulated stdin and never touches the real config or the real installed harness.
The install+remove test snapshots the temporary HOME before and after and compares it, which is
the item's validation: the killswitch leaves the previous state exactly as it was.
"""
import hashlib
import io
import json
import os
import re
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
import i18n  # noqa: E402  (path set above)
import installer_tui  # noqa: E402
import yaml_min  # noqa: E402

BASH = shutil.which("bash") or "/bin/bash"
INSTALL = ROOT / "install.sh"
REMOVE = ROOT / "remove.sh"
INSTALLER = ROOT / "src" / "installer.py"
CLONE_URL = "https://github.com/autonomia-tecnologica/ambiente-de-trabalho.git"
# Stands in for tmux: an attached client and a popup that runs the command string it receives.
FAKE_TMUX = """#!/bin/sh
case "${1:-}" in
  list-clients) printf 'client\\n' ;;
  display-popup)
    shift
    while [ "$#" -gt 1 ]; do shift; done
    command="$1"
    command="${command#\\"}"
    command="${command%\\"}"
    printf '\\n' | /bin/sh -c "$command"
    ;;
esac
"""


def snapshot(root):
    """Every path under root: type, mode and content (or symlink target). Directories included."""
    result = {}
    for current, dirs, files in os.walk(root, followlinks=False):
        for name in dirs + files:
            path = Path(current) / name
            relative = path.relative_to(root).as_posix()
            if path.is_symlink():
                result[relative] = ("link", os.readlink(path))
            elif path.is_dir():
                result[relative] = ("dir", stat.S_IMODE(path.stat().st_mode))
            else:
                result[relative] = ("file", hashlib.sha256(path.read_bytes()).hexdigest(),
                                    stat.S_IMODE(path.stat().st_mode))
    return result


def executable(path, body="#!/bin/sh\nexit 0\n"):
    path.write_text(body, encoding="utf-8")
    path.chmod(0o755)


class InstallerTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name).resolve()
        self.home = self.root / "home"
        self.state = self.root / "state"
        self.bin = self.root / "bin"
        self.source = self.root / "source"
        self.home.mkdir()
        self.state.mkdir()
        self.bin.mkdir()
        (self.source / "regras").mkdir(parents=True)
        (self.source / "HARNESS.md").write_text("# HARNESS\n\nRead the rules.\n", encoding="utf-8")
        (self.source / "CLAUDE.md").write_text("# Harness\n\nRead the rules.\n", encoding="utf-8")
        (self.source / "forcado.yaml").write_text("versao: 1\nitens: []\n", encoding="utf-8")
        (self.source / "ORIGEM.md").write_text("origin\n", encoding="utf-8")
        (self.source / "regras" / "agents.md").write_text("# Agents\n", encoding="utf-8")
        # Deterministic dependency check: required tools exist even on a machine without claude.
        for name in ("tmux", "git", "claude"):
            executable(self.bin / name)
        (self.bin / "python3").symlink_to(sys.executable)
        # A baseline HOME with files the installer must not lose.
        (self.home / ".zshrc").write_text("# my zshrc\n", encoding="utf-8")
        (self.home / ".bashrc").write_text("# my bashrc\n", encoding="utf-8")
        (self.home / ".local" / "bin").mkdir(parents=True)
        executable(self.home / ".local" / "bin" / "other")
        (self.home / ".config").mkdir()
        (self.home / ".config" / "keep.conf").write_text("keep\n", encoding="utf-8")

    def env(self, **overrides):
        env = {key: value for key, value in os.environ.items()
               if key not in ("AT_WS_CONFIG", "AT_WS_INSTALL_STATE", "AT_HARNESS_SOURCE",
                              "AT_WS_PLATFORM", "WS_STATE", "SUDO_USER", "SUDO_UID",
                              "PKEXEC_UID", "DOAS_USER")}
        env.update(HOME=str(self.home), WS_STATE=str(self.state),
                   AT_HARNESS_SOURCE=str(self.source), SHELL="/usr/bin/zsh", NO_COLOR="1",
                   PATH=str(self.bin) + os.pathsep + os.environ.get("PATH", ""))
        for key, value in overrides.items():
            if value is None:
                env.pop(key, None)
            else:
                env[key] = value
        return env

    def run_script(self, script, stdin, **overrides):
        return subprocess.run([BASH, str(script)], input=stdin, env=self.env(**overrides),
                              capture_output=True, text=True, check=False, timeout=60)

    # install, cli, completion, manage (yes), providers (only claude), default (claude), config,
    # harness x2, hooks
    def install(self, stdin="s\ns\ns\ns\ns\n1\n1\ns\ns\ns\ns\n", **overrides):
        return self.run_script(INSTALL, stdin, **overrides)

    def run_lib(self, *args, **overrides):
        return subprocess.run([sys.executable, str(ROOT / "src" / "lib.py"), *args],
                              env=self.env(**overrides), capture_output=True, text=True,
                              check=False, timeout=60)

    def config(self):
        path = self.home / ".config" / "ai-tmux-workspace" / "config.yaml"
        return yaml_min.loads(path.read_text(encoding="utf-8")), path

    def remove(self, stdin="s\n", **overrides):
        return self.run_script(REMOVE, stdin, **overrides)

    def assert_installed(self):
        link = self.home / ".local" / "bin" / "workspace"
        self.assertTrue(link.is_symlink(), "the CLI symlink must exist")
        self.assertEqual(Path(os.readlink(link)), ROOT / "bin" / "workspace")
        self.assertTrue((self.home / ".config" / "ai-tmux-workspace" / "config.yaml").is_file())
        harness = self.home / ".local" / "share" / "ai-tmux-workspace" / "harness"
        for name in ("forcado.yaml", "HARNESS.md", "CLAUDE.md", "regras/agents.md", "source.json"):
            self.assertTrue((harness / name).is_file(), f"missing harness file: {name}")
        for name in ("harness.py", "policy.yaml", "opencode-policy.js", "settings.json"):
            self.assertFalse((harness / name).exists(), f"mechanism must not live in harness/: {name}")
        security = self.home / ".local" / "share" / "ai-tmux-workspace" / "security"
        for name in ("gate.py", "policy.yaml", "opencode-policy.js", "settings.json", "policy.py",
                     "paths.py", "yaml_min.py", "i18n/pt-BR.json"):
            self.assertTrue((security / name).is_file(), f"missing security file: {name}")
        rendered = json.loads((security / "settings.json").read_text(encoding="utf-8"))
        for groups in rendered["hooks"].values():
            for group in groups:
                for hook in group["hooks"]:
                    self.assertIn("security/gate.py", hook["command"])
        self.assertEqual(stat.S_IMODE((harness / "HARNESS.md").stat().st_mode), 0o444)
        self.assertIn(">>> ai-tmux-workspace >>>", (self.home / ".zshrc").read_text(encoding="utf-8"))
        completions = self.home / ".local" / "share" / "ai-tmux-workspace" / "completions"
        self.assertTrue((completions / "workspace.zsh").is_file())
        self.assertTrue((completions / "workspace.bash").is_file())
        claude = self.home / ".claude" / "settings.json"
        self.assertTrue(claude.is_file(), "the Claude Code hooks must be registered")
        text = claude.read_text(encoding="utf-8")
        self.assertIn("hook.py", text)
        self.assertIn("statusLine", text)

    # 1 ---------------------------------------------------------------------------------------
    def test_install_then_remove_restores_the_temporary_home(self):
        before = snapshot(self.home)
        installed = self.install()
        self.assertEqual(installed.returncode, 0, installed.stdout + installed.stderr)
        self.assert_installed()
        # The workspace generates per-profile settings at runtime; the killswitch removes only it.
        (self.state / "settings").mkdir()
        (self.state / "settings" / "claude.json").write_text("{}\n", encoding="utf-8")
        removed = self.remove()
        self.assertEqual(removed.returncode, 0, removed.stdout + removed.stderr)
        self.assertEqual(snapshot(self.home), before)
        self.assertFalse((self.state / "installer").exists())
        self.assertFalse((self.state / "settings").exists())
        self.assertFalse((self.home / ".claude").exists())

    # 2 ---------------------------------------------------------------------------------------
    def test_install_migrates_the_legacy_cache_into_the_control_plane(self):
        legacy = self.home / ".cache" / "workspace" / "tabs"
        legacy.mkdir(parents=True)
        (legacy / "a.json").write_text("old\n", encoding="utf-8")
        installed = self.install(WS_STATE=None)
        self.assertEqual(installed.returncode, 0, installed.stdout + installed.stderr)
        state = self.home / ".config" / "ai-tmux-workspace" / "state"
        self.assertEqual((state / "tabs" / "a.json").read_text(encoding="utf-8"), "old\n")
        self.assertFalse((legacy / "a.json").exists())
        self.assertTrue((self.home / ".config" / "ai-tmux-workspace" / "conference").is_dir())
        self.assertTrue((self.home / ".local" / "share" / "ai-tmux-workspace" / "harness").is_dir())

    def test_aborts_without_confirmation(self):
        before = snapshot(self.home)
        for stdin in ("", "n\n"):
            result = self.install(stdin)
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(snapshot(self.home), before, "nothing may be written before confirming")

    # 3 ---------------------------------------------------------------------------------------
    def test_refuses_to_run_as_root_or_under_sudo(self):
        before = snapshot(self.home)
        under_sudo = self.install("s\n", SUDO_USER="ghost")
        self.assertEqual(under_sudo.returncode, 1)
        self.assertIn("root", under_sudo.stderr.lower())
        self.assertEqual(snapshot(self.home), before)

        unshare = shutil.which("unshare")
        if not unshare:
            self.skipTest("unshare not available to simulate uid 0")
        as_root = subprocess.run(
            [unshare, "-r", BASH, str(INSTALL)], input="s\n", env=self.env(),
            capture_output=True, text=True, check=False, timeout=60)
        self.assertEqual(as_root.returncode, 1, as_root.stdout + as_root.stderr)
        self.assertIn("root", as_root.stderr.lower())
        self.assertEqual(snapshot(self.home), before)

    # 4 ---------------------------------------------------------------------------------------
    def test_missing_harness_offers_the_clone_command_and_declining_aborts(self):
        missing = self.root / "missing-harness"
        before = snapshot(self.home)
        result = self.install("s\ns\ns\ns\ns\n1\n1\ns\nn\n", AT_HARNESS_SOURCE=str(missing))
        self.assertEqual(result.returncode, 1)
        self.assertIn("git clone --depth 1", result.stdout)
        self.assertIn(CLONE_URL, result.stdout)
        self.assertIn(str(missing), result.stdout)
        self.assertFalse(missing.exists())
        # The partial work is in the manifest and the killswitch undoes it.
        removed = self.remove()
        self.assertEqual(removed.returncode, 0, removed.stdout + removed.stderr)
        self.assertEqual(snapshot(self.home), before)

    # 5 ---------------------------------------------------------------------------------------
    def test_remove_asks_before_restoring(self):
        before = snapshot(self.home)
        self.assertEqual(self.install().returncode, 0)
        declined = self.remove("n\n")
        self.assertEqual(declined.returncode, 1)
        self.assertTrue((self.home / ".local" / "bin" / "workspace").is_symlink())
        self.assertEqual(self.remove().returncode, 0)
        self.assertEqual(snapshot(self.home), before)

    # 6 ---------------------------------------------------------------------------------------
    def test_second_install_keeps_the_original_backup(self):
        before = snapshot(self.home)
        self.assertEqual(self.install().returncode, 0)
        self.assertEqual(self.install().returncode, 0)
        self.assertEqual(self.remove().returncode, 0)
        self.assertEqual(snapshot(self.home), before)

    # 7 ---------------------------------------------------------------------------------------
    def test_config_override_is_installed_and_removed(self):
        custom = self.root / "custom" / "config.yaml"
        before = snapshot(self.home)
        result = self.install(AT_WS_CONFIG=str(custom))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue(custom.is_file())
        self.assertFalse((self.home / ".config" / "ai-tmux-workspace" / "config.yaml").exists())
        removed = self.remove(AT_WS_CONFIG=str(custom))
        self.assertEqual(removed.returncode, 0, removed.stdout + removed.stderr)
        self.assertFalse(custom.exists())
        self.assertFalse((self.root / "custom").exists())
        self.assertEqual(snapshot(self.home), before)

    # 8 ---------------------------------------------------------------------------------------
    def test_missing_dependencies_show_the_command_and_require_confirmation(self):
        missing_bin = self.root / "missing-bin"
        missing_bin.mkdir()
        (missing_bin / "python3").symlink_to(sys.executable)
        (missing_bin / "dirname").symlink_to(shutil.which("dirname"))
        (missing_bin / "readlink").symlink_to(shutil.which("readlink"))
        executable(missing_bin / "pacman")
        result = self.install("s\ns\ns\ns\ns\ns\n1\n1\ns\ns\ns\ns\n", PATH=str(missing_bin))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("faltando (obrigatórias): tmux, git, claude", result.stdout)
        self.assertIn("pkexec pacman -S --needed tmux", result.stdout)
        self.assertIn("pkexec pacman -S --needed git", result.stdout)
        self.assertIn("curl -fsSL https://claude.ai/install.sh | bash", result.stdout)
        # The assistant only shows the commands: it must not try to run the package manager.
        self.assertNotIn("password", result.stderr.lower())

    # 9 ---------------------------------------------------------------------------------------
    def test_hooks_restore_existing_settings_and_existing_backup(self):
        claude = self.home / ".claude"
        claude.mkdir()
        original = '{"env": {"KEEP": "1"}}\n'
        original_backup = '{"old": "backup"}\n'
        (claude / "settings.json").write_text(original, encoding="utf-8")
        (claude / "settings.json.bak-workspace").write_text(original_backup, encoding="utf-8")
        before = snapshot(self.home)
        installed = self.install()
        self.assertEqual(installed.returncode, 0, installed.stdout + installed.stderr)
        self.assertIn("hook.py", (claude / "settings.json").read_text(encoding="utf-8"))
        # install-hooks overwrote the old backup with the settings it found.
        self.assertEqual((claude / "settings.json.bak-workspace").read_text(encoding="utf-8"), original)
        removed = self.remove()
        self.assertEqual(removed.returncode, 0, removed.stdout + removed.stderr)
        self.assertEqual((claude / "settings.json").read_text(encoding="utf-8"), original)
        self.assertEqual((claude / "settings.json.bak-workspace").read_text(encoding="utf-8"),
                         original_backup)
        self.assertEqual(snapshot(self.home), before)

    # 10 --------------------------------------------------------------------------------------
    def test_hooks_are_deleted_when_claude_settings_did_not_exist(self):
        before = snapshot(self.home)
        installed = self.install()
        self.assertEqual(installed.returncode, 0, installed.stdout + installed.stderr)
        self.assertTrue((self.home / ".claude" / "settings.json").is_file())
        self.assertFalse((self.home / ".claude" / "settings.json.bak-workspace").exists())
        self.assertEqual(self.remove().returncode, 0)
        self.assertEqual(snapshot(self.home), before)
        self.assertFalse((self.home / ".claude").exists())

    # 11 --------------------------------------------------------------------------------------
    def test_remove_without_manifest_removes_only_the_generated_settings(self):
        before = snapshot(self.home)
        (self.state / "settings").mkdir()
        (self.state / "settings" / "claude.json").write_text("{}\n", encoding="utf-8")
        (self.state / "keep.txt").write_text("keep\n", encoding="utf-8")
        result = self.remove("s\n")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertFalse((self.state / "settings").exists())
        self.assertTrue((self.state / "keep.txt").is_file(), "the rest of the state dir is untouched")
        self.assertEqual(snapshot(self.home), before)

    # 12 --------------------------------------------------------------------------------------
    def test_remove_without_manifest_or_settings_is_not_an_error(self):
        result = self.remove("")
        self.assertEqual(result.returncode, 0)
        self.assertIn(i18n.t("installer.remove.nothing"), result.stdout)

    # 13 --------------------------------------------------------------------------------------
    def test_install_records_source_json_and_locks_the_harness_copy(self):
        installed = self.install()
        self.assertEqual(installed.returncode, 0, installed.stdout + installed.stderr)
        harness = self.home / ".local" / "share" / "ai-tmux-workspace" / "harness"
        record = json.loads((harness / "source.json").read_text(encoding="utf-8"))
        self.assertEqual(record["source"], str(self.source.resolve()))
        self.assertIsNone(record["commit"])
        self.assertEqual(record["source_sha256"],
                         hashlib.sha256((self.source / "HARNESS.md").read_bytes()).hexdigest())
        self.assertEqual(record["installed_sha256"],
                         hashlib.sha256((harness / "HARNESS.md").read_bytes()).hexdigest())
        self.assertEqual(stat.S_IMODE((harness / "HARNESS.md").stat().st_mode), 0o444)

    # 14 --------------------------------------------------------------------------------------
    def test_install_records_the_source_commit_when_it_is_a_git_repo(self):
        git = shutil.which("git")
        if not git:
            self.skipTest("git not available to create the canonical repository")
        subprocess.run([git, "init", "-q", str(self.source)], check=True)
        subprocess.run([git, "-C", str(self.source), "add", "."], check=True)
        subprocess.run([git, "-C", str(self.source), "-c", "user.name=Test", "-c",
                        "user.email=test@example.com", "commit", "--no-verify", "-qm", "source"],
                       check=True)
        head = subprocess.run([git, "-C", str(self.source), "rev-parse", "HEAD"],
                              capture_output=True, text=True, check=True).stdout.strip()
        # The fixture PATH starts with a fake git for the dependency check; the real one comes
        # first here so the installer can read the commit.
        path = str(Path(git).parent) + os.pathsep + self.env()["PATH"]
        installed = self.install(PATH=path)
        self.assertEqual(installed.returncode, 0, installed.stdout + installed.stderr)
        harness = self.home / ".local" / "share" / "ai-tmux-workspace" / "harness"
        record = json.loads((harness / "source.json").read_text(encoding="utf-8"))
        self.assertEqual(record["commit"], head)

    # 15 --------------------------------------------------------------------------------------
    def test_every_literal_i18n_key_exists(self):
        source = INSTALLER.read_text(encoding="utf-8")
        strings = json.loads((ROOT / "i18n" / "pt-BR.json").read_text(encoding="utf-8"))
        keys = set(re.findall(
            r'(?:t|confirm|yes_no|providers|default_profile)\(\s*"(installer[^"]*)"\s*[,)]',
            source))
        keys.update(f"installer.remove.kind.{kind}" for kind in
                    ("file", "dir", "symlink", "absent", "absent_tree", "clone", "settings"))
        keys.update(f"installer.remove.reason.{reason}" for reason in
                    ("outside", "not_empty", "clone_dirty", "clone_url"))
        self.assertTrue(keys)
        self.assertEqual(sorted(key for key in keys if key not in strings), [])

    # 16 --------------------------------------------------------------------------------------
    def test_privileged_outside_tmux_prints_the_command_and_runs_nothing(self):
        marker = self.root / "privileged-ran"
        result = subprocess.run(
            [sys.executable, str(INSTALLER), "--privileged", "sh", "-c", f"touch {marker}"],
            input="s\n", env=self.env(TMUX=""), capture_output=True, text=True, check=False,
            timeout=60)
        self.assertEqual(result.returncode, 125, result.stdout + result.stderr)
        self.assertIn(f"sh -c 'touch {marker}'", result.stdout)
        self.assertFalse(marker.exists(), "nothing privileged may run outside tmux")

    # 17 --------------------------------------------------------------------------------------
    def test_privileged_inside_tmux_runs_on_the_popup_and_returns_the_exit_code(self):
        executable(self.bin / "tmux", FAKE_TMUX)
        result = subprocess.run(
            [sys.executable, str(INSTALLER), "--privileged", "sh", "-c", "exit 7"],
            input="", env=self.env(TMUX="/tmp/fake-tmux,1,0"), capture_output=True, text=True,
            check=False, timeout=60)
        self.assertEqual(result.returncode, 7, result.stdout + result.stderr)

    # 18 --------------------------------------------------------------------------------------
    def test_workspace_privileged_uses_the_same_entry_point(self):
        marker = self.root / "workspace-privileged-ran"
        result = subprocess.run(
            [BASH, str(ROOT / "bin" / "workspace"), "privileged", "sh", "-c", f"touch {marker}"],
            input="s\n", env=self.env(TMUX=""), capture_output=True, text=True, check=False,
            timeout=60)
        self.assertEqual(result.returncode, 125, result.stdout + result.stderr)
        self.assertIn(f"sh -c 'touch {marker}'", result.stdout)
        self.assertFalse(marker.exists())

    # 19 (item 93) ----------------------------------------------------------------------------
    def test_install_without_management_writes_pure_claude_and_no_settings_or_hooks(self):
        before = snapshot(self.home)
        # install, cli, completion, manage (no), harness_managed (yes), providers (only claude),
        # default, config, harness x2
        installed = self.install("s\ns\ns\nn\ns\n1\n1\ns\ns\ns\n")
        self.assertEqual(installed.returncode, 0, installed.stdout + installed.stderr)
        cfg, path = self.config()
        self.assertIs(cfg["manage"], False)
        self.assertIs(cfg["harness_managed"], True)
        self.assertEqual(cfg["profiles"], {"claude": "claude"})
        self.assertEqual(cfg["agent"], "claude")
        harness = self.home / ".local" / "share" / "ai-tmux-workspace" / "harness"
        self.assertTrue((harness / "HARNESS.md").is_file())
        self.assertEqual(stat.S_IMODE((harness / "HARNESS.md").stat().st_mode), 0o444)
        claude_md = self.home / ".claude" / "CLAUDE.md"
        shim = claude_md.read_text(encoding="utf-8")
        self.assertEqual(shim.count(">>> ai-tmux-workspace harness >>>"), 1)
        self.assertIn("@~/.local/share/ai-tmux-workspace/harness/HARNESS.md", shim)
        self.assertEqual(shim.count("@"), 1, "the shim is one import line, nothing else")
        self.assertFalse((self.home / ".claude" / "settings.json").exists(),
                         "manage: false must not register hooks")
        self.assertFalse((self.state / "settings").exists(),
                         "manage: false must not generate settings")
        self.assertIn(i18n.t("installer.hooks.skipped"), installed.stdout)

        launched = self.run_lib("profile-cmd", "claude")
        self.assertEqual(launched.returncode, 0, launched.stderr)
        self.assertEqual(launched.stdout.strip(), "claude")
        for flag in ("--settings", "--permission-mode", "ENABLE_TOOL_SEARCH"):
            self.assertNotIn(flag, launched.stdout)

        ensured = self.run_lib("ensure-settings", "claude")
        self.assertEqual(ensured.returncode, 0, ensured.stderr)
        self.assertEqual(ensured.stdout.strip(), "")
        self.assertFalse((self.state / "settings").exists(),
                         "ensure-settings must not write anything with manage: false")

        repo = self.root / "repo"
        repo.mkdir()
        self.assertEqual(self.run_lib("opening-command", str(repo)).returncode, 0)
        resumed = self.run_lib("opening-command", str(repo))
        self.assertEqual(resumed.returncode, 0, resumed.stderr)
        self.assertIn("--continue", resumed.stdout)
        for flag in ("--settings", "--permission-mode", "ENABLE_TOOL_SEARCH"):
            self.assertNotIn(flag, resumed.stdout)

        removed = self.remove()
        self.assertEqual(removed.returncode, 0, removed.stdout + removed.stderr)
        self.assertEqual(snapshot(self.home), before)

    # 20 (item 107) ---------------------------------------------------------------------------
    def test_install_with_all_providers_writes_the_five_profiles_without_bypass(self):
        # install, cli, completion, manage (yes), all five providers, default (claude), config,
        # harness x2, hooks
        installed = self.install("s\ns\ns\ns\ns\n1 2 3 4 5\n1\ns\ns\ns\ns\n")
        self.assertEqual(installed.returncode, 0, installed.stdout + installed.stderr)
        cfg, path = self.config()
        self.assertIs(cfg["manage"], True)
        self.assertEqual(cfg["agent"], "claude")
        self.assertEqual(set(cfg["profiles"]),
                         {"claude", "opencode-claude", "opencode-deepseek",
                          "opencode-openrouter", "opencode-free"})

        self.assertEqual(cfg["profiles"]["claude"], "claude")
        expected = {
            "opencode-claude": ("opencode-claude.json", "anthropic/claude-sonnet-5-5"),
            "opencode-deepseek": ("opencode-deepseek.json", "deepseek/deepseek-flash"),
            "opencode-openrouter": ("opencode-openrouter.json",
                                    "openrouter/deepseek/deepseek-v4-flash-0731"),
            "opencode-free": ("opencode-free.json", "opencode/deepseek-v4.1-flash-free"),
        }
        for name, (config_name, model) in expected.items():
            profile = cfg["profiles"][name]
            self.assertEqual(profile["harness"], "opencode")
            self.assertEqual(profile["command"], "opencode")
            self.assertIn(config_name, profile["config"])
            self.assertEqual(profile["model"], model)
            self.assertNotIn("settings", profile,
                             "OpenCode profiles carry no Claude settings")
        text = path.read_text(encoding="utf-8")
        self.assertNotIn("bypass", text.lower(), "no profile may carry a bypass")
        profiles_text = yaml_min.dumps(cfg["profiles"]).lower()
        self.assertNotIn("api_key", profiles_text)
        self.assertNotIn("apikeyhelper", profiles_text)
        self.assertNotIn("sk-or-", profiles_text)
        example = (ROOT / "config" / "config.example.yaml").read_text(encoding="utf-8")
        self.assertNotIn("claude-deepseek", example)

        generated = self.run_lib("ensure-settings", "claude")
        self.assertEqual(generated.returncode, 0, generated.stderr)
        settings = json.loads((self.state / "settings" / "claude.json").read_text())
        self.assertNotIn("bypassPermissions", json.dumps(settings))

    # 21 (item 107) ---------------------------------------------------------------------------
    def test_default_profile_is_the_first_marked_when_the_answer_is_empty(self):
        # install, cli, completion, manage, providers (openrouter then claude-harness),
        # default (empty), config, harness x2, hooks
        installed = self.install("s\ns\ns\ns\ns\n4 2\n\ns\ns\ns\ns\n")
        self.assertEqual(installed.returncode, 0, installed.stdout + installed.stderr)
        cfg, _path = self.config()
        self.assertEqual(cfg["agent"], "opencode-claude")
        self.assertEqual(set(cfg["profiles"]), {"opencode-claude", "opencode-openrouter"})

    # 22 (item 107) ---------------------------------------------------------------------------
    def test_default_profile_accepts_an_explicit_choice(self):
        # install, cli, completion, manage, providers (deepseek then free), default (2), config,
        # harness x2, hooks
        installed = self.install("s\ns\ns\ns\ns\n3 5\n2\ns\ns\ns\ns\n")
        self.assertEqual(installed.returncode, 0, installed.stdout + installed.stderr)
        cfg, _path = self.config()
        self.assertEqual(cfg["agent"], "opencode-free")
        self.assertEqual(set(cfg["profiles"]), {"opencode-deepseek", "opencode-free"})

    # 23 (item 93) ----------------------------------------------------------------------------
    def test_existing_config_is_preserved_and_only_manage_agent_and_marked_profiles_change(self):
        path = self.home / ".config" / "ai-tmux-workspace" / "config.yaml"
        path.parent.mkdir(parents=True)
        original = {
            "manage": True,
            "agent": "antigo",
            "main": "~/Codes/main-repo",
            "groups": {"IA": ["~/Codes/a", {"path": "~/Codes/b", "siblings": ["~/Codes/a"]}]},
            "editor": {"prose": [{"name": "micro", "command": "micro", "fullscreen": True}]},
            "prompts": ["p1", "p2"],
            "options": [{"label": "X", "run": "true"}],
            "profiles": {"antigo": "claude --settings ~/.claude/antigo.json"},
        }
        path.write_text(yaml_min.dumps(original), encoding="utf-8")
        before = snapshot(self.home)
        # install, cli, completion, manage (no), harness_managed (yes), providers (only claude),
        # default, update config, harness x2
        installed = self.install("s\ns\ns\nn\ns\n1\n1\ns\ns\ns\n")
        self.assertEqual(installed.returncode, 0, installed.stdout + installed.stderr)
        cfg, _path = self.config()
        self.assertIs(cfg["manage"], False)
        self.assertIs(cfg["harness_managed"], True)
        self.assertEqual(cfg["agent"], "claude")
        self.assertEqual(cfg["profiles"]["antigo"], "claude --settings ~/.claude/antigo.json")
        self.assertEqual(cfg["profiles"]["claude"], "claude")
        for key in ("main", "groups", "editor", "prompts", "options"):
            self.assertEqual(cfg[key], original[key], f"{key} must be preserved")
        removed = self.remove()
        self.assertEqual(removed.returncode, 0, removed.stdout + removed.stderr)
        self.assertEqual(snapshot(self.home), before)

    # 24 (item 95) ----------------------------------------------------------------------------
    def test_installing_twice_with_harness_managed_does_not_duplicate_the_shim(self):
        # install, cli, completion, manage (no), harness_managed (yes), providers, default,
        # config, harness x2
        stdin = "s\ns\ns\nn\ns\n1\n1\ns\ns\ns\n"
        self.assertEqual(self.install(stdin).returncode, 0)
        # The second run updates the existing config and must see the shim already in place.
        second = self.install("s\ns\ns\nn\ns\n1\n1\ns\ns\ns\n")
        self.assertEqual(second.returncode, 0, second.stdout + second.stderr)
        shim = (self.home / ".claude" / "CLAUDE.md").read_text(encoding="utf-8")
        self.assertEqual(shim.count(">>> ai-tmux-workspace harness >>>"), 1)
        self.assertEqual(shim.count("<<< ai-tmux-workspace harness <<<"), 1)
        cfg, _path = self.config()
        self.assertIs(cfg["harness_managed"], True)

    # 25 (item 95) ----------------------------------------------------------------------------
    def test_install_without_harness_management_installs_nothing(self):
        before = snapshot(self.home)
        # install, cli, completion, manage (no), harness_managed (no), providers, default, config
        installed = self.install("s\ns\ns\nn\nn\n1\n1\ns\n")
        self.assertEqual(installed.returncode, 0, installed.stdout + installed.stderr)
        cfg, _path = self.config()
        self.assertIs(cfg["manage"], False)
        self.assertIs(cfg["harness_managed"], False)
        self.assertFalse((self.home / ".local" / "share" / "ai-tmux-workspace" / "harness").exists())
        self.assertFalse((self.home / ".local" / "share" / "ai-tmux-workspace" / "security").exists())
        self.assertFalse((self.home / ".claude").exists())
        self.assertIn(i18n.t("installer.harness_managed.skipped"), installed.stdout)
        removed = self.remove()
        self.assertEqual(removed.returncode, 0, removed.stdout + removed.stderr)
        self.assertEqual(snapshot(self.home), before)

    # 26 (item 107) ---------------------------------------------------------------------------
    def test_existing_config_migrates_the_known_legacy_profile_names(self):
        path = self.home / ".config" / "ai-tmux-workspace" / "config.yaml"
        path.parent.mkdir(parents=True)
        original = {
            "manage": True,
            "agent": "deepseek",
            "main": "~/Codes/main-repo",
            "groups": {"IA": ["~/Codes/a", {"path": "~/Codes/b", "siblings": ["~/Codes/a"]}]},
            "editor": {"code": [{"name": "nvim", "command": "nvim", "fullscreen": False}]},
            "prompts": ["p1", "p2"],
            "profiles": {
                "opencode": {"harness": "opencode", "command": "opencode",
                             "config": "~/legacy/opencode.json",
                             "model": "deepseek/deepseek-flash"},
                "openrouter": {"harness": "opencode", "command": "opencode",
                               "config": "~/legacy/openrouter.json",
                               "model": "openrouter/old"},
                "deepseek": {
                    "command": "claude",
                    "settings": {
                        "env": {"ANTHROPIC_BASE_URL": "https://api.deepseek.com/anthropic",
                                "ANTHROPIC_MODEL": "deepseek-flash"},
                        "apiKeyHelper": "pass show deepseek/api-key",
                    },
                },
                "custom": {"harness": "opencode", "command": "opencode",
                           "config": "~/custom.json", "model": "custom/model"},
            },
        }
        path.write_text(yaml_min.dumps(original), encoding="utf-8")
        # install, cli, completion, manage (no), harness_managed (yes), providers (none), config,
        # harness x2
        installed = self.install("s\ns\ns\nn\ns\n\ns\ns\ns\n")
        self.assertEqual(installed.returncode, 0, installed.stdout + installed.stderr)
        cfg, _path = self.config()
        self.assertEqual(set(cfg["profiles"]),
                         {"opencode-deepseek", "opencode-openrouter", "custom"})
        self.assertEqual(cfg["profiles"]["opencode-deepseek"],
                         original["profiles"]["opencode"],
                         "the key moves; the profile value (config path) does not")
        self.assertEqual(cfg["profiles"]["opencode-openrouter"],
                         original["profiles"]["openrouter"])
        self.assertEqual(cfg["profiles"]["custom"], original["profiles"]["custom"])
        self.assertEqual(cfg["agent"], "opencode-deepseek",
                         "agent follows the removed legacy deepseek profile")
        for key in ("main", "groups", "editor", "prompts"):
            self.assertEqual(cfg[key], original[key], f"{key} must be preserved")

        # Running again is a no-op: nothing else is renamed and the migrated keys stay.
        again = self.install("s\ns\ns\nn\ns\n\ns\ns\ns\n")
        self.assertEqual(again.returncode, 0, again.stdout + again.stderr)
        migrated, _path = self.config()
        self.assertEqual(migrated["profiles"], cfg["profiles"])
        self.assertEqual(migrated["agent"], "opencode-deepseek")

    # 27 (item 107) ---------------------------------------------------------------------------
    def test_a_deepseek_profile_from_another_harness_is_preserved(self):
        path = self.home / ".config" / "ai-tmux-workspace" / "config.yaml"
        path.parent.mkdir(parents=True)
        profile = {"harness": "opencode", "command": "opencode",
                   "config": "~/deepseek.json", "model": "deepseek/deepseek-flash"}
        path.write_text(yaml_min.dumps({
            "manage": True,
            "agent": "deepseek",
            "profiles": {"deepseek": profile},
        }), encoding="utf-8")
        installed = self.install("s\ns\ns\nn\ns\n\ns\ns\ns\n")
        self.assertEqual(installed.returncode, 0, installed.stdout + installed.stderr)
        cfg, _path = self.config()
        self.assertEqual(cfg["profiles"], {"deepseek": profile})
        self.assertEqual(cfg["agent"], "deepseek")

    # 28 (macOS) -------------------------------------------------------------------------------
    def test_install_sh_rejects_python_older_than_39(self):
        fake = self.root / "old-python-bin"
        fake.mkdir()
        # The version probe runs the interpreter, not just `command -v`: a shim that exists and
        # fails (fresh macOS) is reported as an old/broken python3.
        executable(fake / "python3", "#!/bin/sh\nprintf '3.8.0\\n'\nexit 1\n")
        executable(fake / "uname", "#!/bin/sh\nprintf 'Darwin\\n'\n")
        result = self.run_script(INSTALL, "", PATH=str(fake))
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("python3 >= 3.9 is required", result.stderr)
        self.assertIn("found: 3.8.0", result.stderr)
        self.assertIn("xcode-select --install", result.stderr)
        self.assertIn("brew install python", result.stderr)

    # 29 (macOS) -------------------------------------------------------------------------------
    def test_install_sh_reports_a_broken_xcode_python_shim(self):
        fake = self.root / "broken-python-bin"
        fake.mkdir()
        executable(fake / "python3",
                   "#!/bin/sh\nprintf 'xcrun: error: invalid active developer path\\n' >&2\nexit 1\n")
        executable(fake / "uname", "#!/bin/sh\nprintf 'Darwin\\n'\n")
        result = self.run_script(INSTALL, "", PATH=str(fake))
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("python3 >= 3.9 is required (found: unknown)", result.stderr)
        self.assertIn("xcode-select --install", result.stderr)

    # 30 (macOS) -------------------------------------------------------------------------------
    def test_installer_warns_about_old_python_and_tmux_and_repeats_it_at_the_end(self):
        fake = self.root / "old-tools-bin"
        fake.mkdir()
        # Real interpreter for everything except the version probe, so the install still works.
        executable(fake / "python3", f"""#!/bin/sh
case "${{1:-}}" in
  -c) case "${{2:-}}" in
        *version_info*) printf '3.8.0\\n'; exit 0 ;;
      esac ;;
esac
exec {shlex.quote(sys.executable)} "$@"
""")
        executable(fake / "tmux",
                   "#!/bin/sh\n[ \"${1:-}\" = \"-V\" ] && { printf 'tmux 3.3a\\n'; exit 0; }\nexit 0\n")
        executable(fake / "git")
        executable(fake / "claude")
        path = str(fake) + os.pathsep + os.environ.get("PATH", "")
        # The unmet versions add the dependency confirmation: install, deps, cli, completion, ...
        installed = self.install("s\ns\ns\ns\ns\ns\n1\n1\ns\ns\ns\ns\n", PATH=path)
        self.assertEqual(installed.returncode, 0, installed.stdout + installed.stderr)
        self.assertIn("python3 3.8 < 3.9", installed.stdout)
        self.assertIn("tmux 3.3 < 3.4", installed.stdout)
        # The install completes, but finish() repeats the unmet requirements: never a silent success.
        self.assertEqual(installed.stdout.count("tmux 3.3 < 3.4"), 2)
        self.assertEqual(installed.stdout.count("python3 3.8 < 3.9"), 2)

    # 31 (macOS) -------------------------------------------------------------------------------
    def test_darwin_without_a_package_manager_shows_homebrew_and_never_sudo(self):
        fake = self.root / "darwin-bin"
        fake.mkdir()
        (fake / "python3").symlink_to(sys.executable)
        (fake / "dirname").symlink_to(shutil.which("dirname"))
        (fake / "readlink").symlink_to(shutil.which("readlink"))
        result = self.install("s\ns\ns\ns\ns\ns\n1\n1\ns\ns\ns\ns\n", PATH=str(fake),
                              AT_WS_PLATFORM="darwin")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("https://raw.githubusercontent.com/Homebrew/install", result.stdout)
        self.assertIn("brew install bash tmux python", result.stdout)
        # The text may mention sudo (it says the installer never runs it); no line may start with it.
        self.assertFalse(any(line.strip().startswith("sudo") for line in result.stdout.splitlines()))

    # 32 (macOS) -------------------------------------------------------------------------------
    def test_bash_is_searched_in_path_before_bin_bash(self):
        fake = self.root / "bash-bin"
        fake.mkdir()
        executable(fake / "bash", "#!/bin/sh\n[ \"${1:-}\" = \"--version\" ] && "
                                  "{ printf 'GNU bash, version 5.2.15(1)-release\\n'; exit 0; }\n"
                                  "exit 0\n")
        code = "import sys; sys.path.insert(0, 'src'); import installer; print(installer.BASH)"
        result = subprocess.run([sys.executable, "-c", code], cwd=str(ROOT),
                                env=self.env(PATH=str(fake)), capture_output=True, text=True,
                                check=False, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(Path(result.stdout.strip()), fake / "bash")


# Item 75: the curl install (clone to app/), `workspace update`, `workspace --help` and remove.sh taking the
# clone away. The "public repo" is a local git repo built from this tree; nothing here touches the network.
def git(cwd, *args):
    # core.hooksPath=/dev/null: the test repos ignore the machine's global hooks (a commit-msg hook here
    # would refuse the test commits); user.name/email keep commits independent of the global config.
    return subprocess.run(["git", "-c", "user.name=test", "-c", "user.email=test@example.invalid",
                           "-c", "core.hooksPath=/dev/null", *args],
                          cwd=str(cwd), capture_output=True, text=True, check=True)


def make_remote(root):
    remote = root / "remote"
    shutil.copytree(ROOT, remote, ignore=shutil.ignore_patterns(".git", "__pycache__", ".claude"))
    git(remote, "init", "-q", "-b", "main")
    git(remote, "add", "-A")
    git(remote, "commit", "-q", "-m", "initial")
    return remote


class CurlInstallTest(unittest.TestCase):
    ANSWERS = "s\ns\ns\ns\ns\n1\n1\ns\ns\ns\ns\n"   # same answers as InstallerTest.install()

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name).resolve()
        self.home = self.root / "home"
        self.state = self.root / "state"
        self.bin = self.root / "bin"
        self.source = self.root / "source"
        self.tmux_log = self.root / "tmux.log"
        for directory in (self.home, self.state, self.bin, self.source / "regras"):
            directory.mkdir(parents=True)
        (self.source / "HARNESS.md").write_text("# HARNESS\n", encoding="utf-8")
        (self.source / "CLAUDE.md").write_text("# Harness\n", encoding="utf-8")
        (self.source / "forcado.yaml").write_text("versao: 1\nitens: []\n", encoding="utf-8")
        (self.source / "ORIGEM.md").write_text("origin\n", encoding="utf-8")
        (self.source / "regras" / "agents.md").write_text("# Agents\n", encoding="utf-8")
        # tmux and claude are stubs; tmux only logs calls so --help can prove it never opens a session.
        executable(self.bin / "tmux", f"#!/bin/sh\necho called >> '{self.tmux_log}'\nexit 0\n")
        executable(self.bin / "claude")
        (self.bin / "python3").symlink_to(sys.executable)
        (self.home / ".zshrc").write_text("# my zshrc\n", encoding="utf-8")
        self.install_dir = self.home / ".local" / "share" / "ai-tmux-workspace" / "app"

    def env(self, **overrides):
        env = {key: value for key, value in os.environ.items()
               if key not in ("AT_WS_CONFIG", "AT_WS_INSTALL_STATE", "AT_HARNESS_SOURCE", "WS_STATE",
                              "AT_WS_INSTALL_DIR", "AT_WS_REPO_URL", "AT_WS_HARNESS_URL", "TMUX",
                              "SUDO_USER", "SUDO_UID", "PKEXEC_UID", "DOAS_USER")}
        env.update(HOME=str(self.home), WS_STATE=str(self.state), AT_HARNESS_SOURCE=str(self.source),
                   SHELL="/usr/bin/zsh", NO_COLOR="1", AT_WS_NO_TTY="1",
                   PATH=str(self.bin) + os.pathsep + os.environ.get("PATH", ""))
        env.update(overrides)
        return env

    def run_cmd(self, argv, stdin=None, **overrides):
        return subprocess.run(argv, input=stdin, env=self.env(**overrides), capture_output=True,
                              text=isinstance(stdin, str) or stdin is None, check=False, timeout=120)

    def clone(self, remote):
        """What `curl ... | sh` leaves behind: the shallow clone in app/ (AT_WS_REPO_URL points at the local remote)."""
        self.install_dir.parent.mkdir(parents=True)
        git(self.root, "clone", "-q", "--depth", "1", remote.as_uri(), str(self.install_dir))

    # 1 ---------------------------------------------------------------------------------------
    def test_curl_run_clones_shallow_into_app_and_the_assistant_links_it(self):
        remote = make_remote(self.root)
        before = snapshot(self.home)
        # `curl | sh`: the script arrives on stdin. The clone happens; the assistant then gets no answers
        # (the script consumed stdin) and stops without touching HOME beyond the clone.
        piped = self.run_cmd(["sh"], stdin=INSTALL.read_bytes(),
                             AT_WS_REPO_URL=remote.as_uri(), AT_WS_INSTALL_DIR=str(self.install_dir))
        self.assertTrue((self.install_dir / ".git").is_dir(), piped.stdout + piped.stderr)
        self.assertEqual(git(self.install_dir, "rev-parse", "--is-shallow-repository").stdout.strip(), "true")
        self.assertNotEqual(piped.returncode, 0)
        # The assistant from the clone, with answers: the link points into app/.
        installed = self.run_cmd([BASH, str(self.install_dir / "install.sh")], stdin=self.ANSWERS,
                                 AT_WS_REPO_URL=remote.as_uri(), AT_WS_INSTALL_DIR=str(self.install_dir))
        self.assertEqual(installed.returncode, 0, installed.stdout + installed.stderr)
        link = self.home / ".local" / "bin" / "workspace"
        self.assertTrue(link.is_symlink())
        self.assertEqual(Path(os.readlink(link)), self.install_dir / "bin" / "workspace")
        self.assertTrue((self.home / ".config" / "ai-tmux-workspace" / "config.yaml").is_file())
        self.assertFalse((self.home / ".config" / "ai-tmux-workspace" / "app").exists())
        self.assertNotEqual(snapshot(self.home), before)

    # 2 ---------------------------------------------------------------------------------------
    def test_update_shows_by_default_and_fast_forwards_on_apply(self):
        remote = make_remote(self.root)
        self.clone(remote)
        (remote / "NOVO.md").write_text("new\n", encoding="utf-8")
        git(remote, "add", "-A")
        git(remote, "commit", "-q", "-m", "next")
        workspace = str(self.install_dir / "bin" / "workspace")
        # The installed clone is found at its default place (AT_WS_INSTALL_DIR unset), as the layout expects.
        shown = self.run_cmd([BASH, workspace, "update"], AT_WS_REPO_URL=remote.as_uri())
        self.assertEqual(shown.returncode, 0, shown.stdout + shown.stderr)
        self.assertIn("next", shown.stdout)
        self.assertFalse((self.install_dir / "NOVO.md").exists())
        applied = self.run_cmd([BASH, workspace, "update", "--apply"], AT_WS_REPO_URL=remote.as_uri())
        self.assertEqual(applied.returncode, 0, applied.stdout + applied.stderr)
        self.assertEqual((self.install_dir / "NOVO.md").read_text(encoding="utf-8"), "new\n")

    # 3 ---------------------------------------------------------------------------------------
    def test_update_aborts_on_a_dirty_clone_and_changes_nothing(self):
        remote = make_remote(self.root)
        self.clone(remote)
        (remote / "NOVO.md").write_text("new\n", encoding="utf-8")
        git(remote, "add", "-A")
        git(remote, "commit", "-q", "-m", "next")
        (self.install_dir / "README.md").write_text("local edit\n", encoding="utf-8")
        result = self.run_cmd([BASH, str(self.install_dir / "bin" / "workspace"), "update"],
                              AT_WS_INSTALL_DIR=str(self.install_dir))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(i18n.t("cli.update.dirty", root=str(self.install_dir)), result.stderr)
        self.assertFalse((self.install_dir / "NOVO.md").exists())
        self.assertEqual((self.install_dir / "README.md").read_text(encoding="utf-8"), "local edit\n")

    # 4 ---------------------------------------------------------------------------------------
    def test_update_on_a_dev_checkout_shows_the_mode_and_moves_nothing(self):
        remote = make_remote(self.root)          # stands in for a dev checkout (not the app clone)
        self.clone(make_remote(self.root / "other"))
        head = git(remote, "rev-parse", "HEAD").stdout
        result = self.run_cmd([BASH, str(remote / "bin" / "workspace"), "update"],
                              AT_WS_INSTALL_DIR=str(self.install_dir))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn(i18n.t("update.mode.dev", path=str(remote)), result.stdout)
        self.assertEqual(git(remote, "rev-parse", "HEAD").stdout, head)

    # 5 ---------------------------------------------------------------------------------------
    def test_help_prints_the_commands_and_never_opens_tmux(self):
        result = self.run_cmd([BASH, str(ROOT / "bin" / "workspace"), "--help"])
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("update", result.stdout)
        self.assertIn("--help", result.stdout)
        self.assertFalse(self.tmux_log.exists(), "--help must not start or attach tmux")

    # 6 ---------------------------------------------------------------------------------------
    def test_remove_takes_the_clone_and_links_and_leaves_nothing_behind(self):
        remote = make_remote(self.root)
        before = snapshot(self.home)
        self.clone(remote)
        installed = self.run_cmd([BASH, str(self.install_dir / "install.sh")], stdin=self.ANSWERS,
                                 AT_WS_REPO_URL=remote.as_uri(), AT_WS_INSTALL_DIR=str(self.install_dir))
        self.assertEqual(installed.returncode, 0, installed.stdout + installed.stderr)
        # remove.sh lives in the clone; its killswitch needs the answer "s".
        removed = self.run_cmd([BASH, str(self.install_dir / "remove.sh")], stdin="s\n",
                               AT_WS_REPO_URL=remote.as_uri(), AT_WS_INSTALL_DIR=str(self.install_dir))
        self.assertEqual(removed.returncode, 0, removed.stdout + removed.stderr)
        self.assertFalse(self.install_dir.exists())
        self.assertFalse((self.home / ".local" / "share" / "ai-tmux-workspace").exists())
        self.assertEqual(snapshot(self.home), before)

    # 7 ---------------------------------------------------------------------------------------
    def test_remove_never_deletes_a_dev_checkout_or_a_symlink(self):
        remote = make_remote(self.root)
        self.install_dir.parent.mkdir(parents=True)
        self.install_dir.symlink_to(remote, target_is_directory=True)   # a dev checkout behind a symlink
        result = self.run_cmd([BASH, str(remote / "remove.sh")], stdin="s\n",
                              AT_WS_REPO_URL=remote.as_uri(), AT_WS_INSTALL_DIR=str(self.install_dir))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue(remote.is_dir())
        self.assertTrue(self.install_dir.is_symlink())
        self.assertIn("symlink", result.stderr)


class ShimMergeTest(unittest.TestCase):
    """Unit tests of the Claude shim merge: the block is created, replaced in place, and never
    touches the person's text outside the markers."""

    IMPORT = "@~/.local/share/ai-tmux-workspace/harness/HARNESS.md"

    def setUp(self):
        import installer
        self.installer = installer
        self.block = installer.shim_block(self.IMPORT)

    def test_empty_file_gets_only_the_block(self):
        self.assertEqual(self.installer.merge_shim("", self.block), self.block + "\n")

    def test_person_text_is_kept_and_block_appended_once(self):
        user = "# Minhas notas\n\nUma regra minha.\n"
        once = self.installer.merge_shim(user, self.block)
        self.assertEqual(once, user + self.block + "\n")
        self.assertEqual(self.installer.merge_shim(once, self.block), once)

    def test_missing_final_newline_is_handled_before_appending(self):
        once = self.installer.merge_shim("sem fim de linha", self.block)
        self.assertEqual(once, "sem fim de linha\n" + self.block + "\n")

    def test_outdated_block_is_replaced_in_place_and_surroundings_are_kept(self):
        old = ("antes\n" + self.installer.SHIM_START + "\n@~/velho.md\n" +
               self.installer.SHIM_END + "\ndepois\n")
        merged = self.installer.merge_shim(old, self.block)
        self.assertEqual(merged, "antes\n" + self.block + "\ndepois\n")
        self.assertEqual(self.installer.merge_shim(merged, self.block), merged)

    def test_block_without_trailing_newline_is_replaced_without_losing_text(self):
        old = "a\n" + self.installer.SHIM_START + "\n@x\n" + self.installer.SHIM_END
        self.assertEqual(self.installer.merge_shim(old, self.block), "a\n" + self.block)

    def test_repeated_or_unpaired_markers_refuse_to_merge(self):
        start, end = self.installer.SHIM_START, self.installer.SHIM_END
        for damaged in (start + "\n@x\n", "@x\n" + end + "\n", self.block + "\n" + self.block):
            with self.subTest(damaged=damaged), self.assertRaises(self.installer.ShimError):
                self.installer.merge_shim(damaged, self.block)


class ShimRefreshTest(unittest.TestCase):
    """`reload` runs `lib.py shim-refresh`: the same block as install, only with `manage: false`
    (install writes the shim only then). Runs in a throwaway HOME; the person's CLAUDE.md is never used."""

    BLOCK = (">>> ai-tmux-workspace harness >>>\n"
             "@~/.local/share/ai-tmux-workspace/harness/HARNESS.md\n"
             "<<< ai-tmux-workspace harness <<<")

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.home = Path(self._tmp.name).resolve()
        self.state = self.home / "state"
        self.cfg = self.home / "config.yaml"
        self.claude = self.home / ".claude" / "CLAUDE.md"

    def refresh(self, manage):
        self.cfg.write_text(f"manage: {'true' if manage else 'false'}\nagent: claude\n", encoding="utf-8")
        env = {key: value for key, value in os.environ.items()
               if key not in ("AT_WS_INSTALL_DIR", "AT_HARNESS_DIR", "AT_WS_INSTALL_STATE")}
        env.update(HOME=str(self.home), WS_STATE=str(self.state), AT_WS_CONFIG=str(self.cfg),
                   NO_COLOR="1")
        return subprocess.run([sys.executable, str(ROOT / "src" / "lib.py"), "shim-refresh"],
                              env=env, capture_output=True, text=True, check=False, timeout=60)

    def test_reload_creates_the_block_and_a_second_reload_is_a_no_op(self):
        self.assertEqual(self.refresh(manage=False).returncode, 0)
        once = self.claude.read_text(encoding="utf-8")
        self.assertEqual(once, self.BLOCK + "\n")
        self.assertEqual(self.refresh(manage=False).returncode, 0)
        self.assertEqual(self.claude.read_text(encoding="utf-8"), once)

    def test_reload_keeps_the_persons_text_around_the_block(self):
        self.claude.parent.mkdir(parents=True)
        self.claude.write_text("# Minhas regras\n\nUma regra minha.\n", encoding="utf-8")
        self.assertEqual(self.refresh(manage=False).returncode, 0)
        self.assertEqual(self.claude.read_text(encoding="utf-8"),
                         "# Minhas regras\n\nUma regra minha.\n" + self.BLOCK + "\n")
        # An existing block is replaced in place, and the text around it is still the person's.
        old = ">>> ai-tmux-workspace harness >>>\n@~/velho.md\n<<< ai-tmux-workspace harness <<<"
        self.claude.write_text("antes\n" + old + "\ndepois\n", encoding="utf-8")
        self.assertEqual(self.refresh(manage=False).returncode, 0)
        self.assertEqual(self.claude.read_text(encoding="utf-8"), "antes\n" + self.BLOCK + "\ndepois\n")

    def test_reload_warns_and_never_writes_over_a_broken_block(self):
        damaged = "minha nota\n>>> ai-tmux-workspace harness >>>\n@x\n"
        self.claude.parent.mkdir(parents=True)
        self.claude.write_text(damaged, encoding="utf-8")
        result = self.refresh(manage=False)
        self.assertEqual(result.returncode, 0, "reload never fails on the shim")
        self.assertIn("quebrado", result.stderr)
        self.assertEqual(self.claude.read_text(encoding="utf-8"), damaged)

    def test_reload_leaves_the_shim_alone_when_the_workspace_manages_the_harness(self):
        self.assertEqual(self.refresh(manage=True).returncode, 0)
        self.assertFalse(self.claude.exists())


# --- full-screen wizard (item 152) -----------------------------------------------------------

PROVIDER_NAMES = ["claude", "opencode-claude", "opencode-deepseek", "opencode-openrouter", "opencode-free"]


def make_plan(**overrides):
    values = dict(providers=list(PROVIDER_NAMES), dependency_lines=["encontradas: tmux"],
                  cli_link="~/.local/bin/workspace", config_path="~/.config/ai-tmux-workspace/config.yaml",
                  harness_path="~/.local/share/ai-tmux-workspace/harness",
                  settings_path="~/.claude/settings.json", shim_path="~/.claude/CLAUDE.md",
                  clone_url="https://example.invalid/harness.git", harness_missing=False)
    values.update(overrides)
    return installer_tui.Plan(**values)


def drive(keys, plan=None):
    """Feeds key names to a Wizard the way the drawing loop does; returns (wizard, action)."""
    wizard = installer_tui.Wizard(plan or make_plan())
    action = None
    for key in keys:
        action = wizard.handle_key(key)
        if action is not None:
            break
    return wizard, action


class CursesError(Exception):
    pass


FAKE_CURSES = types.SimpleNamespace(error=CursesError, A_BOLD=1, A_REVERSE=2, A_DIM=4,
                                    KEY_UP=259, KEY_DOWN=258, KEY_LEFT=260, KEY_RIGHT=261,
                                    KEY_BACKSPACE=263, KEY_ENTER=343, KEY_PPAGE=339, KEY_NPAGE=338,
                                    ACS_HLINE="-", ACS_VLINE="|", ACS_ULCORNER="+", ACS_URCORNER="+",
                                    ACS_LLCORNER="+", ACS_LRCORNER="+")


class FakeWindow:
    """Records what the drawing layer writes: text cells by (y, x) and the border; writes outside the
    terminal raise like curses does."""

    def __init__(self, height, width):
        self.height, self.width = height, width
        self.cells, self.refreshed = {}, 0

    def erase(self):
        self.cells = {}

    def getmaxyx(self):
        return (self.height, self.width)

    def _check(self, y, x):
        if not (0 <= y < self.height and 0 <= x < self.width):
            raise CursesError("outside the terminal")

    def addnstr(self, y, x, text, n, attr=0):
        self._check(y, x)
        for index, char in enumerate(text[:n]):
            self.cells[(y, x + index)] = (char, attr)

    def addch(self, y, x, char, attr=0):
        self._check(y, x)
        self.cells[(y, x)] = (char, attr)

    def hline(self, y, x, char, n, attr=0):
        for index in range(n):
            self.addch(y, x + index, char, attr)

    def vline(self, y, x, char, n, attr=0):
        for index in range(n):
            self.addch(y + index, x, char, attr)

    def text_row(self, y):
        return "".join(self.cells.get((y, x), (" ", 0))[0] for x in range(self.width))

    def refresh(self):
        self.refreshed += 1


class TuiWizardTest(unittest.TestCase):
    """Screen logic only: no terminal, no install. The keys are the names the drawing layer sends."""

    def test_the_harness_question_is_asked_whatever_manage_says(self):
        for keys in (["enter", "enter", "enter"], ["enter", "enter", "down", "enter"]):
            with self.subTest(keys=keys):
                wizard, _ = drive(keys)
                self.assertEqual(wizard.screen, installer_tui.HARNESS)
                self.assertIn(installer_tui.HARNESS, wizard.screens())

    def test_manage_yes_still_asks_the_harness_and_marks_providers(self):
        wizard, action = drive(["enter", "enter", "enter", "enter", "space", "down", "space", "enter",
                                "enter"])
        self.assertIsNone(action)
        self.assertEqual(wizard.screen, installer_tui.SUMMARY)
        self.assertEqual(wizard.answers, {"manage": True, "harness_managed": True,
                                          "providers": ["claude", "opencode-claude"],
                                          "default": "claude"})

    def test_manage_no_asks_the_harness_and_skips_default_without_providers(self):
        wizard, _ = drive(["enter", "enter", "down", "enter", "down", "enter", "enter"])
        self.assertEqual(wizard.screen, installer_tui.SUMMARY)
        self.assertEqual(wizard.answers, {"manage": False, "harness_managed": False,
                                          "providers": [], "default": None})
        self.assertNotIn(installer_tui.DEFAULT, wizard.screens())

    def test_harness_yes_with_manage_no_keeps_the_harness(self):
        wizard, _ = drive(["enter", "enter", "down", "enter", "enter", "space", "enter"])
        self.assertIs(wizard.answers["manage"], False)
        self.assertIs(wizard.answers["harness_managed"], True)
        self.assertEqual(wizard.answers["providers"], ["claude"])
        self.assertEqual(wizard.answers["default"], "claude")

    def test_the_harness_answer_is_kept_when_manage_is_yes(self):
        wizard, _ = drive(["enter", "enter", "enter", "down", "enter"])
        self.assertIs(wizard.answers["manage"], True)
        self.assertIs(wizard.answers["harness_managed"], False)
        wizard.goto(installer_tui.MANAGE)
        wizard.handle_key("enter")
        self.assertIs(wizard.answers["harness_managed"], False, "manage must not overwrite the answer")

    def test_default_is_always_one_of_the_marked_providers(self):
        wizard, _ = drive(["enter", "enter", "enter", "enter", "space", "down", "down", "space", "enter"])
        self.assertEqual(wizard.answers["providers"], ["claude", "opencode-deepseek"])
        wizard.handle_key("down")
        wizard.handle_key("space")
        self.assertEqual(wizard.answers["default"], "opencode-deepseek")
        wizard.handle_key("b")
        self.assertEqual(wizard.screen, installer_tui.PROVIDERS)
        wizard.handle_key("up")
        wizard.handle_key("space")
        self.assertEqual(wizard.answers["providers"], ["opencode-deepseek"])
        self.assertEqual(wizard.answers["default"], "opencode-deepseek")

    def test_back_returns_to_the_previous_screen_and_welcome_has_none(self):
        wizard = installer_tui.Wizard(make_plan())
        wizard.handle_key("b")
        self.assertEqual(wizard.screen, installer_tui.WELCOME)
        wizard.handle_key("enter")
        wizard.handle_key("enter")
        self.assertEqual(wizard.screen, installer_tui.MANAGE)
        self.assertEqual(wizard.focus, installer_tui.LIST)
        wizard.handle_key("left")      # arrows move between the buttons, they do not go back
        self.assertEqual((wizard.screen, wizard.focus), (installer_tui.MANAGE, installer_tui.BUTTONS))
        wizard.handle_key("enter")     # the focused button is Voltar
        self.assertEqual(wizard.screen, installer_tui.DEPS)

    def test_q_cancels_before_any_install_and_changes_no_answer(self):
        wizard, action = drive(["enter", "enter", "down", "q"])
        self.assertEqual(action, installer_tui.QUIT)
        self.assertEqual(wizard.answers, {"manage": True, "harness_managed": True,
                                          "providers": [], "default": None})

    def test_only_the_summary_starts_the_install_and_the_final_screen_closes(self):
        wizard, action = drive(["enter", "enter", "enter", "enter", "enter"])
        self.assertEqual(wizard.screen, installer_tui.SUMMARY)
        self.assertIsNone(action)
        self.assertEqual(wizard.handle_key("enter"), installer_tui.INSTALL)
        wizard.goto(installer_tui.PROGRESS)
        self.assertIsNone(wizard.handle_key("q"), "keys are ignored while the install runs")
        wizard.goto(installer_tui.DONE)
        self.assertEqual(wizard.handle_key("enter"), installer_tui.CLOSE)

    def test_old_config_skips_every_question(self):
        plan = make_plan(legacy_config="~/.config/ai-tmux-workspace/config.yaml")
        self.assertEqual(installer_tui.Wizard(plan).screens(),
                         [installer_tui.WELCOME, installer_tui.DEPS, installer_tui.SUMMARY,
                          installer_tui.PROGRESS, installer_tui.DONE])

    def test_summary_says_what_will_change_for_each_answer(self):
        def summary(keys):
            wizard, _ = drive(keys)
            return "\n".join(installer_tui.summary_lines(wizard))

        managed = summary(["enter", "enter", "enter", "enter", "space", "enter", "enter"])
        self.assertIn(i18n.t("installer.tui.summary.hooks", path="~/.claude/settings.json"), managed)
        self.assertNotIn(i18n.t("installer.tui.summary.shim", path="~/.claude/CLAUDE.md"), managed)
        unmanaged = summary(["enter", "enter", "down", "enter", "enter", "space", "enter", "enter"])
        self.assertIn(i18n.t("installer.tui.summary.shim", path="~/.claude/CLAUDE.md"), unmanaged)
        self.assertIn(i18n.t("installer.tui.summary.nothing_hooks"), unmanaged)
        none = summary(["enter", "enter", "down", "enter", "down", "enter", "enter"])
        self.assertIn(i18n.t("installer.tui.summary.nothing_harness"), none)
        self.assertIn(i18n.t("installer.tui.summary.nothing_hooks"), none)

    def test_summary_announces_the_clone_only_when_no_harness_copy_exists(self):
        wizard = installer_tui.Wizard(make_plan(harness_missing=True))
        self.assertTrue(any("example.invalid" in line for line in installer_tui.summary_lines(wizard)))
        wizard = installer_tui.Wizard(make_plan(harness_missing=False))
        self.assertFalse(any("example.invalid" in line for line in installer_tui.summary_lines(wizard)))

    def test_view_marks_the_radio_and_the_checkbox_rows(self):
        wizard, _ = drive(["enter", "enter"])
        options = installer_tui.view(wizard, installer_tui.Progress())["options"]
        self.assertEqual([label for label, _mark, _focus in options], [i18n.t("installer.tui.yes"),
                                                                     i18n.t("installer.tui.no")])
        self.assertEqual(options[0][1], "(*)")
        self.assertTrue(options[0][2], "the cursor starts on the marked answer")
        wizard, _ = drive(["enter", "enter", "enter", "enter", "space"])
        marks = [mark for _label, mark, _focus in installer_tui.view(wizard, installer_tui.Progress())["options"]]
        self.assertEqual(marks[0], "[x]")
        self.assertEqual(marks[1], "[ ]")

    def test_layout_is_a_box_centred_in_the_terminal_at_several_sizes(self):
        wizard, _ = drive(["enter", "enter", "enter", "enter", "space", "enter", "enter"])
        view_data = installer_tui.view(wizard, installer_tui.Progress())
        for width, height in ((60, 18), (80, 24), (100, 40), (120, 30), (200, 60)):
            with self.subTest(size=(width, height)):
                geo = installer_tui.layout(view_data, width, height)
                self.assertLessEqual(geo["width"], installer_tui.BOX_MAX_WIDTH)
                self.assertLessEqual(geo["height"], height)
                left_margin, right_margin = geo["left"], width - (geo["left"] + geo["width"])
                top_margin, bottom_margin = geo["top"], height - (geo["top"] + geo["height"])
                self.assertLessEqual(abs(left_margin - right_margin), 1)
                self.assertLessEqual(abs(top_margin - bottom_margin), 1)
                for y, x, text, _style in geo["cells"]:
                    self.assertTrue(geo["top"] < y < geo["top"] + geo["height"] - 1)
                    self.assertTrue(geo["left"] < x and x + len(text) <= geo["left"] + geo["width"] - 1)
        geo = installer_tui.layout(view_data, 60, 18)
        self.assertTrue(any(i18n.t("installer.tui.btn.install") in text for _y, _x, text, _s in geo["cells"]))

    def test_the_focused_option_and_button_are_the_reversed_ones(self):
        wizard, _ = drive(["enter", "enter"])                       # MANAGE, the list has the focus
        geo = installer_tui.layout(installer_tui.view(wizard, installer_tui.Progress()), 80, 24)
        self.assertIn("focus", {style for _y, _x, _t, style in geo["cells"]})
        wizard.handle_key("tab")                                    # the buttons get the focus
        geo = installer_tui.layout(installer_tui.view(wizard, installer_tui.Progress()), 80, 24)
        reversed_text = [text for _y, _x, text, style in geo["cells"] if style == "focus"]
        self.assertEqual(reversed_text, [f"< {i18n.t('installer.tui.btn.next')} >"])

    def test_text_scrolls_inside_the_box_and_clamps(self):
        progress = installer_tui.Progress()
        wizard = installer_tui.Wizard(make_plan())
        wizard.goto(installer_tui.DONE)                              # keys are read on the final screen
        progress.add_section("passo")
        for index in range(30):
            progress.add_line(f"linha {index}")
        geo = installer_tui.layout(installer_tui.view(wizard, progress), 60, 18)
        texts = [text for _y, _x, text, _s in geo["cells"]]
        self.assertIn("   linha 29", texts)                       # tail screens keep the end
        self.assertNotIn("   linha 0", texts)
        wizard.scroll = geo["scroll"]
        wizard.handle_key("pgup", page=3)                          # reads back up
        geo = installer_tui.layout(installer_tui.view(wizard, progress), 60, 18, wizard.scroll)
        texts = [text for _y, _x, text, _s in geo["cells"]]
        self.assertNotIn("   linha 29", texts)
        self.assertEqual(geo["scroll"], wizard.scroll)
        geo = installer_tui.layout(installer_tui.view(wizard, progress), 60, 18, 10_000)
        self.assertEqual(geo["scroll"], max(0, 31 - geo["room"]), "clamped to the last text line")

    def test_option_keys_move_mark_and_the_buttons_act(self):
        wizard, _ = drive(["enter", "enter"])                        # MANAGE, cursor on Sim
        wizard.handle_key("j")
        self.assertEqual(wizard.cursor, 1)
        wizard.handle_key("down")
        self.assertEqual(wizard.cursor, 1, "the cursor stops at the last option")
        wizard.handle_key("k")
        self.assertEqual(wizard.cursor, 0)
        wizard.handle_key("tab")
        self.assertEqual(wizard.focus, installer_tui.BUTTONS)
        self.assertEqual(wizard.handle_key("enter"), None)           # Próximo: moves on
        self.assertEqual(wizard.screen, installer_tui.HARNESS)

    def test_provider_checklist_space_marks_and_enter_only_moves_on(self):
        wizard, _ = drive(["enter", "enter", "enter", "enter"])      # PROVIDERS
        self.assertEqual(wizard.screen, installer_tui.PROVIDERS)
        wizard.handle_key("space")
        wizard.handle_key("down")
        wizard.handle_key("space")
        self.assertEqual(wizard.answers["providers"], ["claude", "opencode-claude"])
        self.assertEqual(wizard.screen, installer_tui.PROVIDERS)
        wizard.handle_key("enter")
        self.assertEqual(wizard.screen, installer_tui.DEFAULT)
        self.assertEqual(wizard.answers["providers"], ["claude", "opencode-claude"])

    def test_small_terminal_shows_the_warning_and_never_raises(self):
        wizard, _ = drive(["enter", "enter", "enter", "enter", "space", "enter", "enter"])
        window = FakeWindow(17, 59)
        self.assertIsNone(installer_tui.draw(FAKE_CURSES, window, installer_tui.view(wizard, installer_tui.Progress())))
        self.assertIn("Mínimo 60x18", window.text_row(0))
        tiny = FakeWindow(1, 4)
        installer_tui.draw(FAKE_CURSES, tiny, installer_tui.view(wizard, installer_tui.Progress()))

    def test_run_falls_back_to_text_when_the_terminal_is_too_small(self):
        original_curses, original_size = installer_tui._curses, installer_tui._terminal_size
        installer_tui._curses = lambda: FAKE_CURSES
        installer_tui._terminal_size = lambda: (59, 17)
        try:
            with mock.patch("sys.stdout", new=io.StringIO()) as printed:
                self.assertIsNone(installer_tui.run(make_plan(), lambda answers, progress: 0))
            self.assertIn("Mínimo 60x18", printed.getvalue())
        finally:
            installer_tui._curses, installer_tui._terminal_size = original_curses, original_size

    def test_final_screen_titles_follow_the_install_result(self):
        wizard = installer_tui.Wizard(make_plan())
        wizard.goto(installer_tui.DONE)
        progress = installer_tui.Progress()
        progress.finish(0)
        ok = installer_tui.view(wizard, progress)
        self.assertEqual(ok["title"], i18n.t("installer.tui.title.done_ok"))
        self.assertEqual(ok["buttons"], [i18n.t("installer.tui.btn.finish")])
        progress.finish(1)
        failed = installer_tui.view(wizard, progress)
        self.assertEqual(failed["title"], i18n.t("installer.tui.title.done_failed"))
        self.assertEqual(failed["hint"], i18n.t("installer.tui.hint.failed"))

    def test_every_screen_renders_without_a_key_error(self):
        plan = make_plan(dependency_lines=["faltando (obrigatórias): tmux"])
        for keys in ([], ["enter"], ["enter", "enter"], ["enter", "enter", "down", "enter"],
                     ["enter", "enter", "enter", "enter", "space", "enter"],
                     ["enter", "enter", "enter", "enter", "space", "enter", "enter"],
                     ["enter", "enter", "enter", "enter", "space", "enter", "enter", "enter"]):
            wizard, _ = drive(keys, plan)
            with self.subTest(screen=wizard.screen):
                self.assertTrue(installer_tui.view(wizard, installer_tui.Progress())["title"])
        wizard = installer_tui.Wizard(plan)
        for screen in (installer_tui.PROGRESS, installer_tui.DONE):
            wizard.goto(screen)
            with self.subTest(screen=screen):
                self.assertTrue(installer_tui.view(wizard, installer_tui.Progress())["hint"])

    def test_progress_tail_keeps_the_latest_lines(self):
        progress = installer_tui.Progress()
        progress.add_section("passo")
        for index in range(30):
            progress.add_line(f"linha {index}")
        wizard = installer_tui.Wizard(make_plan())
        wizard.goto(installer_tui.PROGRESS)
        geo = installer_tui.layout(installer_tui.view(wizard, progress), 60, 10)
        self.assertIn("   linha 29", [text for _y, _x, text, _s in geo["cells"]])

    def test_progress_redraws_on_every_change_and_keeps_multiline_text(self):
        progress = installer_tui.Progress()
        calls = []
        progress.listener = lambda: calls.append(1)
        progress.add_section("A")
        progress.add_line("um\ndois")
        progress.finish(0)
        self.assertEqual(len(calls), 3)
        self.assertEqual(progress.rows(), ["A", "   um", "   dois"])
        self.assertEqual(progress.code, 0)

    def test_key_names_come_from_curses_codes_and_printable_keys(self):
        names = {code: installer_tui.key_name(FAKE_CURSES, code)
                 for code in (259, 258, 260, 261, 339, 338, 9, 263, 343, 10, 27, 32, 127, 66, 9999)}
        self.assertEqual(names, {259: "up", 258: "down", 260: "left", 261: "right", 339: "pgup",
                                 338: "pgdn", 9: "tab", 263: "backspace", 343: "enter", 10: "enter",
                                 27: None, 32: "space", 127: "backspace", 66: "b", 9999: None})

    def test_esc_alone_never_cancels_the_wizard(self):
        wizard, action = drive(["enter", "enter", "esc"])
        self.assertIsNone(action)
        self.assertEqual(wizard.screen, installer_tui.MANAGE)

    def test_draw_centres_the_box_and_redraws_at_a_new_size(self):
        wizard, _ = drive(["enter", "enter", "enter", "enter", "space", "enter", "enter"])
        view_data = installer_tui.view(wizard, installer_tui.Progress())
        for height, width in ((24, 80), (40, 120)):
            window = FakeWindow(height, width)
            geo = installer_tui.draw(FAKE_CURSES, window, view_data)
            self.assertEqual(window.refreshed, 1)
            self.assertEqual(window.cells[(geo["top"], geo["left"])][0], "+")
            self.assertEqual(geo["left"], (width - geo["width"]) // 2)
            self.assertEqual(geo["top"], (height - geo["height"]) // 2)
        tiny = FakeWindow(1, 4)
        installer_tui.draw(FAKE_CURSES, tiny, view_data)

    def test_run_refuses_without_curses_so_the_text_mode_takes_over(self):
        original = installer_tui._curses
        installer_tui._curses = lambda: None
        try:
            self.assertIsNone(installer_tui.run(make_plan(), lambda answers, progress: 0))
            self.assertFalse(installer_tui.can_run(io.StringIO(), io.StringIO(), {"TERM": "xterm"}))
        finally:
            installer_tui._curses = original

    def test_can_run_needs_a_terminal_both_ways_and_a_real_term(self):
        class Tty(io.StringIO):
            def isatty(self):
                return True

        original = installer_tui._curses
        installer_tui._curses = lambda: FAKE_CURSES
        try:
            self.assertTrue(installer_tui.can_run(Tty(), Tty(), {"TERM": "xterm-256color"}))
            self.assertFalse(installer_tui.can_run(io.StringIO(), Tty(), {"TERM": "xterm"}))
            self.assertFalse(installer_tui.can_run(Tty(), io.StringIO(), {"TERM": "xterm"}))
            self.assertFalse(installer_tui.can_run(Tty(), Tty(), {"TERM": "dumb"}))
            self.assertFalse(installer_tui.can_run(Tty(), Tty(), {}))
        finally:
            installer_tui._curses = original

    def test_every_literal_tui_key_exists_in_pt_br(self):
        source = (ROOT / "src" / "installer_tui.py").read_text(encoding="utf-8")
        strings = json.loads((ROOT / "i18n" / "pt-BR.json").read_text(encoding="utf-8"))
        # Literal keys, plus the ones built at run time (title per screen, help per question) and the
        # branches of a conditional expression, which the literal scan reads only in part.
        keys = set(re.findall(r'\bt\(\s*"(installer\.tui[^"]*)"', source))
        keys.update(f"installer.tui.title.{name}" for name in
                    ("welcome", "deps", "manage", "harness", "providers", "default", "summary",
                     "progress", "done_ok", "done_failed"))
        keys.update(f"installer.tui.{name}.help" for name in ("manage", "harness", "providers", "default"))
        keys.update(f"installer.tui.hint.{name}" for name in ("radio", "providers", "failed", "done"))
        keys = {key for key in keys if not key.endswith(".")}
        self.assertTrue(keys)
        self.assertEqual(sorted(key for key in keys if key not in strings), [])


class TuiInstallTest(unittest.TestCase):
    """The wizard's answers drive the same Assistant as the text mode: config and manifest are checked."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name).resolve()
        self.home, self.state = self.root / "home", self.root / "state"
        self.bin, self.source = self.root / "bin", self.root / "source"
        for directory in (self.home, self.state, self.bin, self.source / "regras"):
            directory.mkdir(parents=True)
        (self.source / "HARNESS.md").write_text("# HARNESS\n", encoding="utf-8")
        (self.source / "CLAUDE.md").write_text("# Harness\n", encoding="utf-8")
        (self.source / "forcado.yaml").write_text("versao: 1\nitens: []\n", encoding="utf-8")
        (self.source / "regras" / "agents.md").write_text("# Agents\n", encoding="utf-8")
        for name in ("tmux", "git", "claude"):
            executable(self.bin / name)
        (self.bin / "python3").symlink_to(sys.executable)
        (self.home / ".zshrc").write_text("# my zshrc\n", encoding="utf-8")

    def env(self):
        env = {key: value for key, value in os.environ.items()
               if key not in ("AT_WS_CONFIG", "AT_WS_INSTALL_STATE", "AT_HARNESS_SOURCE",
                              "AT_WS_PLATFORM", "WS_STATE", "SUDO_USER", "SUDO_UID",
                              "PKEXEC_UID", "DOAS_USER", "TERM")}
        env.update(HOME=str(self.home), WS_STATE=str(self.state), AT_HARNESS_SOURCE=str(self.source),
                   SHELL="/usr/bin/zsh", NO_COLOR="1", TERM="dumb",
                   PATH=str(self.bin) + os.pathsep + os.environ.get("PATH", ""))
        return env

    def run_wizard(self, keys):
        """Walks the wizard with `keys`, then runs the wizard's install in a child process (HOME is the
        temporary one; the module reads it at import)."""
        wizard, action = drive(keys, make_plan(providers=PROVIDER_NAMES))
        self.assertEqual(action, installer_tui.INSTALL, "the keys must reach the summary and install")
        script = (
            "import json, sys\n"
            f"sys.path.insert(0, {str(ROOT / 'src')!r})\n"
            "import installer, installer_tui\n"
            "answers = json.loads(sys.argv[1])\n"
            "progress = installer_tui.Progress()\n"
            "code = installer.perform_tui(answers, progress)\n"
            "print(json.dumps({'code': code, 'steps': [title for title, _lines in progress.steps]}))\n"
        )
        result = subprocess.run([sys.executable, "-c", script, json.dumps(wizard.answers)],
                                env=self.env(), capture_output=True, text=True, check=False, timeout=120)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return json.loads(result.stdout.strip().splitlines()[-1]), result

    def config(self):
        path = self.home / ".config" / "ai-tmux-workspace" / "config.yaml"
        return yaml_min.loads(path.read_text(encoding="utf-8")), path

    def manifest(self):
        return json.loads((self.state / "installer" / "manifest.json").read_text(encoding="utf-8"))

    def test_manage_yes_and_harness_yes_write_config_harness_hooks_and_manifest(self):
        outcome, _ = self.run_wizard(["enter", "enter", "enter", "enter", "space", "enter", "enter", "enter"])
        self.assertEqual(outcome["code"], 0)
        self.assertIn(i18n.t("installer.title"), outcome["steps"])
        cfg, _path = self.config()
        self.assertIs(cfg["manage"], True)
        self.assertIs(cfg["harness_managed"], True)
        self.assertEqual(cfg["profiles"], {"claude": "claude"})
        self.assertEqual(cfg["agent"], "claude")
        harness = self.home / ".local" / "share" / "ai-tmux-workspace" / "harness"
        self.assertTrue((harness / "HARNESS.md").is_file())
        self.assertTrue((self.home / ".claude" / "settings.json").is_file())
        self.assertTrue(any(entry["path"].endswith("config.yaml") for entry in self.manifest()["entries"]))

    def test_manage_yes_and_harness_no_writes_the_answer_and_no_harness_copy(self):
        outcome, _ = self.run_wizard(["enter", "enter", "enter", "down", "enter", "enter", "enter"])
        self.assertEqual(outcome["code"], 0)
        cfg, _path = self.config()
        self.assertIs(cfg["manage"], True)
        self.assertIs(cfg["harness_managed"], False)
        self.assertFalse((self.home / ".local" / "share" / "ai-tmux-workspace" / "harness").exists())
        self.assertTrue((self.home / ".claude" / "settings.json").is_file(), "manage still registers hooks")

    def test_manage_no_and_harness_no_install_no_harness_and_no_hooks(self):
        outcome, _ = self.run_wizard(["enter", "enter", "down", "enter", "down", "enter", "enter", "enter"])
        self.assertEqual(outcome["code"], 0)
        cfg, _path = self.config()
        self.assertIs(cfg["manage"], False)
        self.assertIs(cfg["harness_managed"], False)
        self.assertFalse((self.home / ".local" / "share" / "ai-tmux-workspace" / "harness").exists())
        self.assertFalse((self.home / ".claude").exists())
        self.assertTrue(self.manifest()["entries"], "the manifest records what was touched")

    def test_manage_no_and_harness_yes_imports_the_harness_without_hooks(self):
        outcome, _ = self.run_wizard(["enter", "enter", "down", "enter", "enter", "space", "enter",
                                      "enter", "enter"])
        self.assertEqual(outcome["code"], 0)
        cfg, _path = self.config()
        self.assertIs(cfg["manage"], False)
        self.assertIs(cfg["harness_managed"], True)
        shim = (self.home / ".claude" / "CLAUDE.md").read_text(encoding="utf-8")
        self.assertEqual(shim.count(">>> ai-tmux-workspace harness >>>"), 1)
        self.assertFalse((self.home / ".claude" / "settings.json").exists())
        self.assertFalse((self.state / "settings").exists())

    def test_the_text_mode_runs_with_text_flag_and_when_stdin_is_not_a_terminal(self):
        for args, extra in ((["--text"], {"TERM": "xterm-256color"}), ([], {"TERM": "dumb"}),
                            ([], {"TERM": "xterm-256color"})):
            with self.subTest(args=args, extra=extra):
                env = self.env()
                env.update(extra)
                result = subprocess.run([sys.executable, str(INSTALLER), *args], input="n\n",
                                        env=env, capture_output=True, text=True, check=False, timeout=60)
                self.assertEqual(result.returncode, 1)
                self.assertIn(i18n.t("installer.install.confirm"), result.stdout)
                self.assertNotIn("\x1b[?1049h", result.stdout)


if __name__ == "__main__":
    unittest.main()
