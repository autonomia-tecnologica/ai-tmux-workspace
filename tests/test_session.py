"""Tests for the session declaration (item 64): the role/root prefix, the main repo as `home`, the
workspace code as `self`, and the conference left without a role. No capability policy reads it any
more; these tests cover the declaration only.

Every test uses a temporary HOME, WS_STATE, AT_WS_CONFIG and WS_DIR, so the real config, cache and
workspace code are never read or written. The launcher paths are exercised as child processes, the
same way `bin/workspace` calls them."""
import contextlib
import io
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
LIB = SRC / "lib.py"
SESSION = SRC / "session.py"

sys.path.insert(0, str(SRC))

import session  # noqa: E402

ENV_KEYS = (
    "HOME", "WS_STATE", "AT_WS_CONFIG", "WS_DIR",
    "WS_SESSION_ROLE", "WS_SESSION_ROOT", "WS_SESSION_ID", "TMPDIR",
)


@contextlib.contextmanager
def patched_env(**values):
    saved = {name: os.environ.get(name) for name in values}
    for name, value in values.items():
        if value is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = str(value)
    try:
        yield
    finally:
        for name, value in saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


class SessionTest(unittest.TestCase):
    def setUp(self):
        # /tmp is inside every box, so a temp repo there would mask the outside-the-box cases.
        base = "/var/tmp" if os.access("/var/tmp", os.W_OK) else None
        self._tmp = tempfile.TemporaryDirectory(dir=base)
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name).resolve()
        self.home = self.root / "home"
        self.state = self.root / "state"
        self.main = self.root / "main"
        self.repo = self.root / "repo"
        self.other = self.root / "other"
        self.code = self.root / "workspace-code"
        for folder in (self.home / ".config", self.state, self.main, self.repo, self.other,
                       self.code / "src", self.root / "config"):
            folder.mkdir(parents=True)
        self.config = self.root / "config" / "config.json"
        self.config.write_text(json.dumps({
            "main": str(self.main),
            "agent": "claude",
            "profiles": {"claude": "claude"},
        }), encoding="utf-8")
        self.saved = {name: os.environ.pop(name, None) for name in ENV_KEYS}
        os.environ.update(HOME=str(self.home), WS_STATE=str(self.state),
                          AT_WS_CONFIG=str(self.config), WS_DIR=str(self.code))
        self.addCleanup(self._restore)

    def _restore(self):
        for name, value in self.saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value

    def env(self, **overrides):
        env = {name: value for name, value in os.environ.items() if name not in ENV_KEYS}
        env.update(HOME=str(self.home), WS_STATE=str(self.state),
                   AT_WS_CONFIG=str(self.config), WS_DIR=str(self.code))
        for name, value in overrides.items():
            if value is None:
                env.pop(name, None)
            else:
                env[name] = str(value)
        return env

    def lib(self, *args, **overrides):
        return subprocess.run([sys.executable, str(LIB), *args], env=self.env(**overrides),
                              capture_output=True, text=True, check=False)

    def session_cli(self, *args, **overrides):
        return subprocess.run([sys.executable, str(SESSION), *args], env=self.env(**overrides),
                              capture_output=True, text=True, check=False)

    # --- contract: src/session.py ---------------------------------------------------------------
    def test_main_repo_is_home_and_every_other_path_is_repo(self):
        self.assertEqual(session.environment(str(self.main)), {
            session.ROLE: "home", session.ROOT: str(self.main)})
        self.assertEqual(session.environment(str(self.repo)), {
            session.ROLE: "repo", session.ROOT: str(self.repo)})

    def test_root_is_the_real_path_of_the_session(self):
        link = self.root / "link"
        link.symlink_to(self.repo)
        self.assertEqual(session.environment(str(link))[session.ROOT], str(self.repo))

    # --- the dev window of the workspace's own code (item 85) ------------------------------------
    def test_workspace_code_root_is_the_self_session(self):
        self.assertEqual(session.environment(str(self.code)), {
            session.ROLE: "self", session.ROOT: str(self.code)})
        self.assertEqual(session.prefix(str(self.code)),
                         f"env WS_SESSION_ROLE=self WS_SESSION_ROOT={self.code}")
        # only the root itself is the dev window: a folder inside the code is a plain repo
        self.assertEqual(session.environment(str(self.code / "src"))[session.ROLE], "repo")
        self.assertEqual(session.environment(str(self.repo))[session.ROLE], "repo")

    def test_home_wins_over_self_when_the_main_repo_is_the_workspace(self):
        self.config.write_text(json.dumps({
            "main": str(self.code), "agent": "claude", "profiles": {"claude": "claude"},
        }), encoding="utf-8")
        self.assertEqual(session.environment(str(self.code))[session.ROLE], "home")
        self.assertEqual(session.environment(str(self.repo))[session.ROLE], "repo")

    def test_reload_keeps_the_dev_window_by_its_name(self):
        # prune closes windows whose name is not in window-names; the dev window is not in config
        result = self.lib("window-names", "claude")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(self.code.name, result.stdout.splitlines())

    def test_dev_without_a_running_workspace_fails_clearly(self):
        profile = f"no-such-dev-{os.getpid()}"
        process = subprocess.run([str(ROOT / "bin" / "workspace"), "dev", profile],
                                 env=self.env(), capture_output=True, text=True, check=False)
        self.assertEqual(process.returncode, 1, process.stderr)
        self.assertIn(profile, process.stderr)

    def test_without_a_repo_there_is_no_environment(self):
        self.assertEqual(session.environment(""), {})
        self.assertEqual(session.environment(None), {})
        self.assertEqual(session.prefix(None), "")

    def test_prefix_uses_the_env_form_the_two_callers_understand(self):
        self.assertEqual(session.prefix(str(self.repo)),
                         f"env WS_SESSION_ROLE=repo WS_SESSION_ROOT={self.repo}")
        self.assertEqual(shlex.split(session.prefix(str(self.repo))),
                         ["env", "WS_SESSION_ROLE=repo", f"WS_SESSION_ROOT={self.repo}"])

    def test_prefix_with_profile_adds_the_profile_after_role_and_root(self):
        self.assertEqual(
            session.prefix(str(self.repo), profile="deepseek"),
            f"env WS_SESSION_ROLE=repo WS_SESSION_ROOT={self.repo} WS_SESSION_PROFILE=deepseek")
        self.assertEqual(shlex.split(session.prefix(str(self.repo), profile="deepseek")),
                         ["env", "WS_SESSION_ROLE=repo", f"WS_SESSION_ROOT={self.repo}",
                          "WS_SESSION_PROFILE=deepseek"])

    def test_prefix_with_profile_quotes_special_characters(self):
        self.assertEqual(
            session.prefix(str(self.repo), profile="my profile"),
            f"env WS_SESSION_ROLE=repo WS_SESSION_ROOT={self.repo} WS_SESSION_PROFILE='my profile'")

    def test_prefix_with_tab_adds_the_session_tab_and_state_dir(self):
        with patched_env(WS_STATE=str(self.state)):
            self.assertEqual(
                session.prefix(str(self.repo), profile="deepseek", tab="tab-1"),
                f"env WS_SESSION_ROLE=repo WS_SESSION_ROOT={self.repo} "
                f"WS_SESSION_PROFILE=deepseek WS_SESSION_TAB=tab-1 WS_STATE={self.state}")
        self.assertEqual(
            shlex.split(session.prefix(str(self.repo), profile="deepseek", tab="tab-1")),
            ["env", "WS_SESSION_ROLE=repo", f"WS_SESSION_ROOT={self.repo}",
             "WS_SESSION_PROFILE=deepseek", "WS_SESSION_TAB=tab-1", f"WS_STATE={self.state}"])

    def test_prefix_with_tab_without_ws_state_uses_the_default_state_dir(self):
        with patched_env(WS_STATE=None):
            self.assertEqual(
                session.prefix(str(self.repo), tab="tab-1"),
                f"env WS_SESSION_ROLE=repo WS_SESSION_ROOT={self.repo} "
                f"WS_SESSION_TAB=tab-1 WS_STATE={self.config.parent / 'state'}")

    def test_prefix_reads_the_main_repo_from_the_config(self):
        self.assertEqual(session.prefix(str(self.main)),
                         f"env WS_SESSION_ROLE=home WS_SESSION_ROOT={self.main}")

    def test_prefix_reads_a_yaml_config_too(self):
        yaml_config = self.root / "config.yaml"
        yaml_config.write_text(f"main: {self.main}\n", encoding="utf-8")
        with patched_env(AT_WS_CONFIG=str(yaml_config)):
            self.assertEqual(session.prefix(str(self.main)),
                             f"env WS_SESSION_ROLE=home WS_SESSION_ROOT={self.main}")

    def test_broken_config_falls_back_to_the_repo_role(self):
        broken = self.root / "broken.yaml"
        broken.write_text("main: [unclosed\n", encoding="utf-8")
        with patched_env(AT_WS_CONFIG=str(broken)):
            self.assertEqual(session.environment(str(self.main))[session.ROLE], "repo")

    def test_cli_prints_the_same_prefix(self):
        proc = self.session_cli("env", str(self.repo))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.strip(),
                         f"env WS_SESSION_ROLE=repo WS_SESSION_ROOT={self.repo}")
        proc = self.session_cli("env", str(self.main))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.strip(),
                         f"env WS_SESSION_ROLE=home WS_SESSION_ROOT={self.main}")
        proc = self.session_cli("env", str(self.repo), "deepseek", "tab-1")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("WS_SESSION_PROFILE=deepseek WS_SESSION_TAB=tab-1", proc.stdout)
        self.assertIn(f"WS_STATE={self.state}", proc.stdout)

    # --- prefix in the launch line --------------------------------------------------------------
    def test_opening_command_and_profile_cmd_pass_the_repo(self):
        opened = self.lib("opening-command", str(self.repo))
        self.assertEqual(opened.returncode, 0, opened.stderr)
        self.assertTrue(opened.stdout.strip().startswith(
            f"env WS_SESSION_ROLE=repo WS_SESSION_ROOT={self.repo} "))

        profile = self.lib("profile-cmd", "claude", str(self.repo))
        self.assertEqual(profile.returncode, 0, profile.stderr)
        self.assertTrue(profile.stdout.strip().startswith(
            f"env WS_SESSION_ROLE=repo WS_SESSION_ROOT={self.repo} "))

        without = self.lib("profile-cmd", "claude")
        self.assertEqual(without.returncode, 0, without.stderr)
        self.assertNotIn("WS_SESSION_ROLE", without.stdout)

    # --- agent binary found without the interactive PATH (item 105) -----------------------------
    def opencode_free(self):
        self.config.write_text(json.dumps({
            "main": str(self.main),
            "agent": "opencode-free",
            "profiles": {
                "claude": "claude",
                "opencode-free": {"harness": "opencode", "command": "opencode",
                                  "config": "~/.config/ai-tmux-workspace/opencode-free.json"},
            },
        }), encoding="utf-8")

    def fake_opencode(self):
        bindir = self.home / ".opencode" / "bin"
        bindir.mkdir(parents=True)
        binary = bindir / "opencode"
        binary.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        binary.chmod(0o755)
        return bindir, binary

    @staticmethod
    def launch_env(line):
        return dict(arg.split("=", 1) for arg in shlex.split(line) if "=" in arg)

    @staticmethod
    def system_bin_dirs():
        """macOS also appends the Homebrew folders that exist (lib.HOMEBREW_BIN_DIRS); Linux adds none."""
        if sys.platform != "darwin":
            return []
        return [d for d in ("/opt/homebrew/bin", "/opt/homebrew/sbin", "/usr/local/bin", "/usr/local/sbin")
                if os.path.isdir(d)]

    def test_opencode_launch_adds_the_existing_user_bin_dirs_to_path(self):
        self.opencode_free()
        bindir, binary = self.fake_opencode()
        path = f"/usr/bin{os.pathsep}/bin"
        proc = self.lib("profile-cmd", "opencode-free", str(self.repo), PATH=path)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        out = proc.stdout.strip()
        exports = self.launch_env(out)
        self.assertEqual(exports["PATH"], os.pathsep.join([path, str(bindir), *self.system_bin_dirs()]))
        self.assertEqual(shutil.which("opencode", path=exports["PATH"]), str(binary))
        self.assertEqual(exports["OPENCODE_CONFIG"],
                         str(self.state / "settings" / "opencode-opencode-free.json"))
        self.assertTrue(out.endswith(" opencode --standalone"))

    def test_opencode_launch_does_not_repeat_bin_dirs_already_in_path(self):
        self.opencode_free()
        bindir, _ = self.fake_opencode()
        extra = self.home / "bin"
        extra.mkdir()
        path = f"{bindir}{os.pathsep}/usr/bin"
        proc = self.lib("profile-cmd", "opencode-free", str(self.repo), PATH=path)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        exports = self.launch_env(proc.stdout.strip())
        self.assertEqual(exports["PATH"], os.pathsep.join([path, str(extra), *self.system_bin_dirs()]))
        self.assertEqual(exports["PATH"].split(os.pathsep).count(str(bindir)), 1)

    def test_opencode_launch_keeps_the_recorded_identity(self):
        self.opencode_free()
        bindir, _ = self.fake_opencode()
        proc = self.lib("session-command", "opencode-free", str(self.repo),
                        PATH=f"/usr/bin{os.pathsep}/bin")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        saved = json.loads((self.state / "agent.json").read_text(encoding="utf-8"))
        self.assertEqual(saved[str(self.repo)]["command"], "opencode [opencode-free]")
        self.assertNotIn(str(bindir), json.dumps(saved))

    def test_claude_launch_does_not_add_the_path(self):
        (self.home / ".opencode" / "bin").mkdir(parents=True)
        proc = self.lib("profile-cmd", "claude", str(self.repo),
                        PATH=f"/usr/bin{os.pathsep}/bin")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertNotIn("PATH=", proc.stdout)

    def test_identity_is_recorded_without_the_session_prefix(self):
        self.assertEqual(self.lib("opening-command", str(self.main)).returncode, 0)
        self.assertEqual(self.lib("opening-command", str(self.repo)).returncode, 0)
        saved = json.loads((self.state / "permission.json").read_text(encoding="utf-8"))
        self.assertEqual(saved[str(self.main)]["claude"]["command"], "claude")
        self.assertEqual(saved[str(self.repo)]["claude"]["command"], "claude")
        self.assertNotIn("WS_SESSION_ROLE", json.dumps(saved))
        self.assertNotIn("WS_SESSION_PROFILE", json.dumps(saved))

    def test_options_repo_and_command_passes_the_window_repo(self):
        import options
        with mock.patch.object(options.tmux, "display", return_value=str(self.repo)), \
                mock.patch.object(options.lib, "agent_launch_command",
                                  return_value="claude") as launch:
            repo, argv = options.repo_and_command("@1")
        self.assertEqual(repo, str(self.repo))
        launch.assert_called_once_with(repo=str(self.repo), profile="claude")
        self.assertEqual(argv, ["claude"])

    def test_the_conference_does_not_receive_a_role(self):
        import conference_worker
        with patched_env(AT_WS_CONFIG=str(self.config), WS_STATE=str(self.state),
                         WS_DIR=str(self.code), HOME=str(self.home),
                         WS_CONFERENCE=str(self.root / "conference"),
                         AT_HARNESS_DIR=str(self.root / "no-harness")):
            command = conference_worker.answer_command([str(self.other)])
        self.assertNotIn("WS_SESSION_ROLE", command)
        self.assertNotIn("WS_SESSION_ROOT", command)


if __name__ == "__main__":
    unittest.main()
