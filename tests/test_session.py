"""Tests for the session box (item 64): the role/root prefix, the main repo as `home`, the box
enforced by the same policy in both adapters, and the conference left without a role.

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
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
LIB = SRC / "lib.py"
SESSION = SRC / "session.py"
PLUGIN = ROOT / "harness" / "opencode-policy.js"

sys.path.insert(0, str(SRC))

import i18n  # noqa: E402
import policy  # noqa: E402
import session  # noqa: E402

ENV_KEYS = (
    "HOME", "WS_STATE", "AT_WS_CONFIG", "WS_DIR", "WS_POLICY", "WS_POLICY_AUTO", "WS_AUTO",
    "WS_SESSION_ROLE", "WS_SESSION_ROOT", "WS_SESSION_PARENT_ROLE", "WS_PARENT_ROLE",
    "WS_SESSION_ID", "TMPDIR",
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
                       self.code / "src"):
            folder.mkdir(parents=True)
        self.config = self.root / "config.json"
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

    def read(self, path, **extra):
        return {"tool_name": "Read", "tool_input": {"file_path": str(path)},
                "session_id": "sess-1", **extra}

    def write(self, path, **extra):
        return {"tool_name": "Write", "tool_input": {"file_path": str(path)},
                "session_id": "sess-1", **extra}

    def decide(self, event, **overrides):
        with patched_env(**overrides):
            return policy.check_event(event)

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
                f"WS_SESSION_TAB=tab-1 WS_STATE={self.home / '.cache' / 'workspace'}")

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

    def test_opencode_launch_adds_the_existing_user_bin_dirs_to_path(self):
        self.opencode_free()
        bindir, binary = self.fake_opencode()
        path = f"/usr/bin{os.pathsep}/bin"
        proc = self.lib("profile-cmd", "opencode-free", str(self.repo), PATH=path)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        out = proc.stdout.strip()
        exports = self.launch_env(out)
        self.assertEqual(exports["PATH"], f"{path}{os.pathsep}{bindir}")
        self.assertEqual(shutil.which("opencode", path=exports["PATH"]), str(binary))
        self.assertEqual(exports["OPENCODE_CONFIG"],
                         str(self.home / ".config" / "ai-tmux-workspace" / "opencode-free.json"))
        self.assertTrue(out.endswith(" opencode"))

    def test_opencode_launch_does_not_repeat_bin_dirs_already_in_path(self):
        self.opencode_free()
        bindir, _ = self.fake_opencode()
        extra = self.home / "bin"
        extra.mkdir()
        path = f"{bindir}{os.pathsep}/usr/bin"
        proc = self.lib("profile-cmd", "opencode-free", str(self.repo), PATH=path)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        exports = self.launch_env(proc.stdout.strip())
        self.assertEqual(exports["PATH"], f"{path}{os.pathsep}{extra}")
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

    # --- the box in the policy -----------------------------------------------------------------
    def test_repo_session_writes_only_its_own_repo_and_tmp(self):
        cases = [
            (self.write(self.repo / "a.py"), "repo.write", "allow"),
            (self.write(self.other / "a.py"), "repo.outside", "deny"),
            (self.write(self.home / ".config" / "app.conf"), "config.write", "deny"),
            (self.write(self.home / ".claude" / "settings.json"), "config.write", "deny"),
            (self.write("/tmp/session-box-test.txt"), "tmp", "allow"),
        ]
        for event, capability, decision in cases:
            with self.subTest(event=event):
                result = self.decide(event, WS_SESSION_ROLE="repo",
                                     WS_SESSION_ROOT=str(self.repo))
                self.assertEqual((result["capability"], result["decision"]),
                                 (capability, decision))

    def test_auto_does_not_change_the_box_decisions(self):
        cases = [self.write(self.repo / "a.py"), self.write(self.other / "a.py"),
                 self.write(self.home / ".config" / "app.conf"), self.write("/tmp/auto.txt")]
        for event in cases:
            with self.subTest(event=event):
                without = self.decide(event, WS_SESSION_ROLE="repo",
                                      WS_SESSION_ROOT=str(self.repo))
                with_auto = self.decide(event, WS_SESSION_ROLE="repo",
                                        WS_SESSION_ROOT=str(self.repo), WS_POLICY_AUTO="1")
                self.assertEqual(without["decision"], with_auto["decision"])

    def test_home_can_change_the_other_repos(self):
        result = self.decide(self.write(self.other / "a.py"), WS_SESSION_ROLE="home",
                             WS_SESSION_ROOT=str(self.main))
        self.assertEqual((result["capability"], result["decision"]), ("repo.outside", "allow"))

    def test_worktree_under_the_config_root_writes_its_own_tree(self):
        worktree = (self.home / ".local" / "share" / "ai-tmux-workspace" / "worktrees" /
                    "repo" / "branch")
        sibling = (self.home / ".local" / "share" / "ai-tmux-workspace" / "worktrees" /
                   "repo" / "other")
        worktree.mkdir(parents=True)
        sibling.mkdir(parents=True)
        inside = self.decide(self.write(worktree / "a.py"), WS_SESSION_ROLE="repo",
                             WS_SESSION_ROOT=str(worktree))
        self.assertEqual((inside["capability"], inside["decision"]), ("repo.write", "allow"))
        outside = self.decide(self.write(sibling / "a.py"), WS_SESSION_ROLE="repo",
                              WS_SESSION_ROOT=str(worktree))
        self.assertEqual((outside["capability"], outside["decision"]),
                         ("config.write", "deny"))
        config = self.decide(self.write(self.home / ".local" / "share" / "ai-tmux-workspace" /
                                        "other.txt"), WS_SESSION_ROLE="repo",
                             WS_SESSION_ROOT=str(worktree))
        self.assertEqual((config["capability"], config["decision"]),
                         ("config.write", "deny"))

    def test_secrets_still_win_inside_the_box(self):
        for path in (self.repo / ".env", self.repo / ".ssh" / "id_rsa",
                     self.repo / "server.pem"):
            with self.subTest(path=path):
                result = self.decide(self.read(path), WS_SESSION_ROLE="repo",
                                     WS_SESSION_ROOT=str(self.repo))
                self.assertEqual((result["capability"], result["decision"]),
                                 ("secrets", "deny"))

    # --- fail closed in the hook ---------------------------------------------------------------
    def test_hook_denies_when_the_engine_fails_with_a_declared_role(self):
        import hook

        def broken(_event):
            raise RuntimeError("engine down")

        fake = SimpleNamespace(check_event=broken)
        with patched_env(WS_SESSION_ROLE="repo", WS_SESSION_ROOT=str(self.repo)):
            with mock.patch.dict(sys.modules, {"policy": fake}):
                out = io.StringIO()
                with contextlib.redirect_stdout(out):
                    hook._policy_check(self.write(self.repo / "a.py"))
        payload = json.loads(out.getvalue())
        self.assertEqual(payload["hookSpecificOutput"]["permissionDecision"], "deny")
        self.assertEqual(payload["hookSpecificOutput"]["permissionDecisionReason"],
                         i18n.t("policy.reason.error", capability="-"))

    def test_hook_keeps_sessions_without_a_role_untouched(self):
        import hook

        def broken(_event):
            raise RuntimeError("engine down")

        with patched_env(WS_SESSION_ROLE=None):
            with mock.patch.dict(sys.modules, {"policy": SimpleNamespace(check_event=broken)}):
                out = io.StringIO()
                with contextlib.redirect_stdout(out):
                    hook._policy_check(self.write(self.repo / "a.py"))
        self.assertEqual(out.getvalue(), "")

    def test_hook_denies_an_invalid_engine_answer_with_a_declared_role(self):
        import hook
        with patched_env(WS_SESSION_ROLE="repo", WS_SESSION_ROOT=str(self.repo)):
            with mock.patch.dict(sys.modules, {"policy": SimpleNamespace(
                    check_event=lambda _event: {"decision": "maybe"})}):
                out = io.StringIO()
                with contextlib.redirect_stdout(out):
                    hook._policy_check(self.write(self.repo / "a.py"))
        self.assertEqual(json.loads(out.getvalue())["hookSpecificOutput"]["permissionDecision"],
                         "deny")


if __name__ == "__main__":
    unittest.main()
