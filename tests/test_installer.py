"""Tests for the step-by-step installer (`install.sh` + `src/installer.py`) and its killswitch.

Every test uses a temporary HOME, a temporary state dir and a fake harness source, drives the
assistant with simulated stdin and never touches the real config or the real installed harness.
The install+remove test snapshots the temporary HOME before and after and compares it, which is
the item's validation: the killswitch leaves the previous state exactly as it was.
"""
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BASH = shutil.which("bash") or "/bin/bash"
INSTALL = ROOT / "install.sh"
REMOVE = ROOT / "remove.sh"
INSTALLER = ROOT / "src" / "installer.py"
CLONE_URL = "https://github.com/autonomia-tecnologica/ambiente-de-trabalho.git"


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
                              "WS_ESTADO", "SUDO_USER", "SUDO_UID", "PKEXEC_UID", "DOAS_USER")}
        env.update(HOME=str(self.home), WS_ESTADO=str(self.state),
                   AT_HARNESS_SOURCE=str(self.source), SHELL="/usr/bin/zsh", NO_COLOR="1",
                   PATH=str(self.bin) + os.pathsep + os.environ.get("PATH", ""))
        env.update(overrides)
        return env

    def run_script(self, script, stdin, **overrides):
        return subprocess.run([BASH, str(script)], input=stdin, env=self.env(**overrides),
                              capture_output=True, text=True, check=False, timeout=60)

    def install(self, stdin="s\ns\ns\ns\ns\ns\ns\n", **overrides):
        return self.run_script(INSTALL, stdin, **overrides)

    def remove(self, stdin="s\n", **overrides):
        return self.run_script(REMOVE, stdin, **overrides)

    def assert_installed(self):
        link = self.home / ".local" / "bin" / "workspace"
        self.assertTrue(link.is_symlink(), "the CLI symlink must exist")
        self.assertEqual(Path(os.readlink(link)), ROOT / "bin" / "workspace")
        self.assertTrue((self.home / ".config" / "ai-tmux-workspace" / "config.yaml").is_file())
        harness = self.home / ".local" / "share" / "ai-tmux-workspace" / "harness"
        for name in ("forcado.yaml", "harness.py", "HARNESS.md", "CLAUDE.md", "regras/agents.md"):
            self.assertTrue((harness / name).is_file(), f"missing harness file: {name}")
        self.assertTrue((harness / "settings.json").is_file())
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
        result = self.install("s\ns\ns\ns\nn\n", AT_HARNESS_SOURCE=str(missing))
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
        result = self.install("s\ns\ns\ns\ns\ns\ns\ns\n", PATH=str(missing_bin))
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
        self.assertIn("nada a remover", result.stdout)

    # 13 --------------------------------------------------------------------------------------
    def test_every_literal_i18n_key_exists(self):
        source = INSTALLER.read_text(encoding="utf-8")
        strings = json.loads((ROOT / "i18n" / "pt-BR.json").read_text(encoding="utf-8"))
        keys = set(re.findall(r'(?:t|confirm)\(\s*"(installer[^"]*)"\s*[,)]', source))
        keys.update(f"installer.remove.kind.{kind}" for kind in
                    ("file", "dir", "symlink", "absent", "absent_tree", "clone", "settings"))
        keys.update(f"installer.remove.reason.{reason}" for reason in
                    ("outside", "not_empty", "clone_dirty", "clone_url"))
        self.assertTrue(keys)
        self.assertEqual(sorted(key for key in keys if key not in strings), [])


if __name__ == "__main__":
    unittest.main()
