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
import shlex
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
                              "AT_WS_PLATFORM", "WS_STATE", "SUDO_USER", "SUDO_UID",
                              "PKEXEC_UID", "DOAS_USER")}
        env.update(HOME=str(self.home), WS_STATE=str(self.state),
                   AT_HARNESS_SOURCE=str(self.source), SHELL="/usr/bin/zsh", NO_COLOR="1",
                   PATH=str(self.bin) + os.pathsep + os.environ.get("PATH", ""))
        env.update(overrides)
        return env

    def run_script(self, script, stdin, **overrides):
        return subprocess.run([BASH, str(script)], input=stdin, env=self.env(**overrides),
                              capture_output=True, text=True, check=False, timeout=60)

    # install, cli, completion, manage (yes), providers (only claude), default (claude), config,
    # harness x2, hooks
    def install(self, stdin="s\ns\ns\ns\n1\n1\ns\ns\ns\ns\n", **overrides):
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
                     "yaml_min.py", "i18n/pt-BR.json"):
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
        result = self.install("s\ns\ns\ns\n1\n1\ns\nn\n", AT_HARNESS_SOURCE=str(missing))
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
        result = self.install("s\ns\ns\ns\ns\n1\n1\ns\ns\ns\ns\n", PATH=str(missing_bin))
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

    # 20 (item 107) ---------------------------------------------------------------------------
    def test_install_with_all_providers_writes_the_five_profiles_without_bypass(self):
        # install, cli, completion, manage (yes), all five providers, default (claude), config,
        # harness x2, hooks
        installed = self.install("s\ns\ns\ns\n1 2 3 4 5\n1\ns\ns\ns\ns\n")
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
        installed = self.install("s\ns\ns\ns\n4 2\n\ns\ns\ns\ns\n")
        self.assertEqual(installed.returncode, 0, installed.stdout + installed.stderr)
        cfg, _path = self.config()
        self.assertEqual(cfg["agent"], "opencode-claude")
        self.assertEqual(set(cfg["profiles"]), {"opencode-claude", "opencode-openrouter"})

    # 22 (item 107) ---------------------------------------------------------------------------
    def test_default_profile_accepts_an_explicit_choice(self):
        # install, cli, completion, manage, providers (deepseek then free), default (2), config,
        # harness x2, hooks
        installed = self.install("s\ns\ns\ns\n3 5\n2\ns\ns\ns\ns\n")
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
        installed = self.install("s\ns\ns\ns\ns\n1\n1\ns\ns\ns\ns\n", PATH=path)
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
        result = self.install("s\ns\ns\ns\ns\n1\n1\ns\ns\ns\ns\n", PATH=str(fake),
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
    ANSWERS = "s\ns\ns\ns\n1\n1\ns\ns\ns\ns\n"   # same answers as InstallerTest.install()

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
    def test_update_fast_forwards_a_clean_clone(self):
        remote = make_remote(self.root)
        self.clone(remote)
        (remote / "NOVO.md").write_text("new\n", encoding="utf-8")
        git(remote, "add", "-A")
        git(remote, "commit", "-q", "-m", "next")
        result = self.run_cmd([BASH, str(self.install_dir / "bin" / "workspace"), "update"],
                              AT_WS_INSTALL_DIR=str(self.install_dir))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
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
    def test_update_refuses_a_dev_checkout_and_leaves_it_alone(self):
        remote = make_remote(self.root)          # stands in for a dev checkout (not the app clone)
        self.clone(make_remote(self.root / "other"))
        head = git(remote, "rev-parse", "HEAD").stdout
        result = self.run_cmd([BASH, str(remote / "bin" / "workspace"), "update"],
                              AT_WS_INSTALL_DIR=str(self.install_dir))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(i18n.t("cli.update.dev", root=str(remote)), result.stderr)
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


if __name__ == "__main__":
    unittest.main()
