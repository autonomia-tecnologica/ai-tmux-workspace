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
sys.path.insert(0, str(ROOT / "src"))
import i18n  # noqa: E402  (path set above)
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
                              "WS_STATE", "SUDO_USER", "SUDO_UID", "PKEXEC_UID", "DOAS_USER")}
        env.update(HOME=str(self.home), WS_STATE=str(self.state),
                   AT_HARNESS_SOURCE=str(self.source), SHELL="/usr/bin/zsh", NO_COLOR="1",
                   PATH=str(self.bin) + os.pathsep + os.environ.get("PATH", ""))
        env.update(overrides)
        return env

    def run_script(self, script, stdin, **overrides):
        return subprocess.run([BASH, str(script)], input=stdin, env=self.env(**overrides),
                              capture_output=True, text=True, check=False, timeout=60)

    # install, cli, completion, manage (yes), providers (only claude), config, harness x2, hooks
    def install(self, stdin="s\ns\ns\ns\n1\ns\ns\ns\ns\n", **overrides):
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
        for name in ("forcado.yaml", "harness.py", "HARNESS.md", "CLAUDE.md", "regras/agents.md",
                     "source.json"):
            self.assertTrue((harness / name).is_file(), f"missing harness file: {name}")
        self.assertTrue((harness / "settings.json").is_file())
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
        result = self.install("s\ns\ns\ns\n1\ns\nn\n", AT_HARNESS_SOURCE=str(missing))
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
        result = self.install("s\ns\ns\ns\ns\n1\ns\ns\ns\ns\n", PATH=str(missing_bin))
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
        keys = set(re.findall(r'(?:t|confirm|yes_no|providers)\(\s*"(installer[^"]*)"\s*[,)]', source))
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
        # config, harness x2
        installed = self.install("s\ns\ns\nn\ns\n1\ns\ns\ns\n")
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
        self.assertIn("~/.local/share/ai-tmux-workspace/harness/HARNESS.md", shim)
        self.assertIn(i18n.t("installer.harness.shim"), shim)
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

    # 20 (item 93) ----------------------------------------------------------------------------
    def test_install_with_all_providers_writes_the_four_profiles_without_bypass(self):
        # install, cli, completion, manage (yes), all four providers, config, harness x2, hooks
        installed = self.install("s\ns\ns\ns\n1 2 3 4\ns\ns\ns\ns\n")
        self.assertEqual(installed.returncode, 0, installed.stdout + installed.stderr)
        cfg, path = self.config()
        self.assertIs(cfg["manage"], True)
        self.assertEqual(cfg["agent"], "claude")
        self.assertEqual(set(cfg["profiles"]),
                         {"claude", "claude-deepseek", "opencode-deepseek", "opencode-free"})

        deepseek = cfg["profiles"]["claude-deepseek"]
        self.assertEqual(deepseek["command"], "claude")
        self.assertEqual(deepseek["settings"]["env"]["ANTHROPIC_BASE_URL"],
                         "https://api.deepseek.com/anthropic")
        self.assertEqual(deepseek["settings"]["apiKeyHelper"], "pass show deepseek/api-key")
        self.assertNotIn("permissions", deepseek)
        self.assertNotIn("ANTHROPIC_AUTH_TOKEN", deepseek["settings"]["env"])
        text = path.read_text(encoding="utf-8")
        self.assertNotIn("bypass", text.lower(), "no profile may carry a bypass")

        opencode = cfg["profiles"]["opencode-deepseek"]
        self.assertEqual(opencode["harness"], "opencode")
        self.assertEqual(opencode["command"], "opencode")
        self.assertIn("opencode-deepseek.json", opencode["config"])
        self.assertEqual(opencode["model"], "deepseek/deepseek-flash")
        free = cfg["profiles"]["opencode-free"]
        self.assertEqual(free["harness"], "opencode")
        self.assertIn("opencode-free.json", free["config"])
        self.assertEqual(free["model"], "opencode/deepseek-v4.1-flash-free")
        for profile in (opencode, free):
            self.assertNotIn("settings", profile, "OpenCode profiles carry no Claude settings")

        generated = self.run_lib("ensure-settings", "claude-deepseek")
        self.assertEqual(generated.returncode, 0, generated.stderr)
        settings = json.loads((self.state / "settings" / "claude-deepseek.json").read_text())
        self.assertEqual(settings["env"]["ANTHROPIC_BASE_URL"],
                         "https://api.deepseek.com/anthropic")
        self.assertNotIn("bypassPermissions", json.dumps(settings))

    # 21 (item 93) ----------------------------------------------------------------------------
    def test_agent_is_the_first_marked_provider_in_canonical_order(self):
        installed = self.install("s\ns\ns\ns\n4 2\ns\ns\ns\ns\n")
        self.assertEqual(installed.returncode, 0, installed.stdout + installed.stderr)
        cfg, _path = self.config()
        self.assertEqual(cfg["agent"], "claude-deepseek")
        self.assertEqual(set(cfg["profiles"]), {"claude-deepseek", "opencode-free"})

    # 22 (item 93) ----------------------------------------------------------------------------
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
        # update config, harness x2
        installed = self.install("s\ns\ns\nn\ns\n1\ns\ns\ns\n")
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

    # 23 (item 95) ----------------------------------------------------------------------------
    def test_installing_twice_with_harness_managed_does_not_duplicate_the_shim(self):
        # install, cli, completion, manage (no), harness_managed (yes), providers, config, harness x2
        stdin = "s\ns\ns\nn\ns\n1\ns\ns\ns\n"
        self.assertEqual(self.install(stdin).returncode, 0)
        # The second run updates the existing config and must see the shim already in place.
        second = self.install("s\ns\ns\nn\ns\n1\ns\ns\ns\n")
        self.assertEqual(second.returncode, 0, second.stdout + second.stderr)
        shim = (self.home / ".claude" / "CLAUDE.md").read_text(encoding="utf-8")
        self.assertEqual(shim.count(">>> ai-tmux-workspace harness >>>"), 1)
        self.assertEqual(shim.count("<<< ai-tmux-workspace harness <<<"), 1)
        cfg, _path = self.config()
        self.assertIs(cfg["harness_managed"], True)

    # 24 (item 95) ----------------------------------------------------------------------------
    def test_install_without_harness_management_installs_nothing(self):
        before = snapshot(self.home)
        # install, cli, completion, manage (no), harness_managed (no), providers, config
        installed = self.install("s\ns\ns\nn\nn\n1\ns\n")
        self.assertEqual(installed.returncode, 0, installed.stdout + installed.stderr)
        cfg, _path = self.config()
        self.assertIs(cfg["manage"], False)
        self.assertIs(cfg["harness_managed"], False)
        self.assertFalse((self.home / ".local" / "share" / "ai-tmux-workspace" / "harness").exists())
        self.assertFalse((self.home / ".claude").exists())
        self.assertIn(i18n.t("installer.harness_managed.skipped"), installed.stdout)
        removed = self.remove()
        self.assertEqual(removed.returncode, 0, removed.stdout + removed.stderr)
        self.assertEqual(snapshot(self.home), before)


if __name__ == "__main__":
    unittest.main()
