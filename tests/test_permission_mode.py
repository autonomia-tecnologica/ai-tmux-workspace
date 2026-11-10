"""Tests for the last permission mode per repo (D10): acceptEdits on the first opening of any Claude
harness profile (including a custom endpoint such as `claude-deepseek`), never bypass by default, the
last mode reapplied for the same agent command, and a fallback on profile switches.

Every test uses a temporary HOME, WS_STATE and AT_WS_CONFIG, so the real cache and config are never
read or written. `lib.py` and `hook.py` run in child processes, like the workspace runs them."""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
sys.path.insert(0, str(SRC))

import permission_state  # noqa: E402


class PermissionModeTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name).resolve()
        self.home = self.root / "home"
        self.state = self.root / "state"
        self.home.mkdir()
        self.state.mkdir()
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.config = self.root / "config.json"
        self.set_command("claude", "claude")
        env = mock.patch.dict(os.environ, {"WS_STATE": str(self.state)})
        env.start()
        self.addCleanup(env.stop)

    def set_command(self, command, agent="a"):
        self.agent = agent
        self.command = command
        self.config.write_text(
            json.dumps({"agent": agent, "profiles": {agent: command}}), encoding="utf-8")

    def env(self):
        return dict(os.environ, HOME=str(self.home), WS_STATE=str(self.state),
                    AT_WS_CONFIG=str(self.config))

    def opening(self, repo=None):
        proc = subprocess.run(
            [sys.executable, str(SRC / "lib.py"), "opening-command", str(repo or self.repo)],
            env=self.env(), capture_output=True, text=True, check=False)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return proc.stdout.strip()

    def profile_cmd(self, repo=""):
        argv = [sys.executable, str(SRC / "lib.py"), "profile-cmd", self.agent]
        if repo:
            argv.append(str(repo))
        proc = subprocess.run(argv, env=self.env(), capture_output=True, text=True, check=False)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return proc.stdout.strip()

    def saved(self):
        return permission_state.read()

    def test_first_opening_uses_accept_edits(self):
        out = self.opening()
        self.assertIn("--permission-mode acceptEdits", out)
        entry = self.saved()[str(self.repo)]["claude"]
        self.assertEqual(entry["command"], self.command)
        self.assertEqual(entry["mode"], "acceptEdits")

    def test_last_mode_is_reapplied_for_the_same_command(self):
        self.opening()
        permission_state.record_mode(str(self.repo), "bypassPermissions")
        out = self.opening()
        self.assertIn("--permission-mode bypassPermissions", out)

    def test_auto_is_reapplied_in_the_claude_profile(self):
        self.opening()
        permission_state.record_mode(str(self.repo), "auto")
        out = self.opening()
        self.assertIn("--permission-mode auto", out)

    def test_mode_from_a_running_session_takes_the_command_from_agent_state(self):
        (self.state / "agent.json").write_text(
            json.dumps({str(self.repo): {"command": self.command}}), encoding="utf-8")
        permission_state.record_mode(str(self.repo), "bypassPermissions")
        self.assertEqual(permission_state.mode_for(str(self.repo), self.command, True),
                         "bypassPermissions")

    def test_custom_claude_profile_first_opening_uses_accept_edits(self):
        self.set_command("claude --settings ~/.claude/settings.deepseek.json", agent="claude-deepseek")
        out = self.opening()
        self.assertIn("--permission-mode acceptEdits", out)
        self.assertNotIn("bypassPermissions", out)
        entry = self.saved()[str(self.repo)]["claude-deepseek"]
        self.assertEqual(entry["mode"], "acceptEdits")

    def test_custom_claude_profile_reapplies_auto(self):
        self.set_command("claude --settings ~/.claude/settings.deepseek.json", agent="claude-deepseek")
        self.opening()
        permission_state.record_mode(str(self.repo), "auto", profile="claude-deepseek")
        out = self.opening()
        self.assertIn("--permission-mode auto", out)

    def test_profile_switch_does_not_apply_the_saved_mode_first(self):
        self.opening()
        permission_state.record_mode(str(self.repo), "auto")
        self.set_command("claude --settings ~/.claude/settings.other.json", agent="other")
        out = self.opening()
        self.assertIn("--permission-mode acceptEdits", out)
        self.assertNotIn("--permission-mode auto", out)

    def test_custom_claude_profile_keeps_a_mode_chosen_in_the_session(self):
        self.set_command("claude --settings ~/.claude/settings.deepseek.json", agent="claude-deepseek")
        self.opening()
        permission_state.record_mode(str(self.repo), "plan", profile="claude-deepseek")
        out = self.opening()
        self.assertIn("--permission-mode plan", out)

    def test_profile_cmd_without_repo_has_no_mode(self):
        self.assertNotIn("--permission-mode", self.profile_cmd())

    def test_profile_cmd_with_repo_uses_the_saved_mode(self):
        self.opening()
        permission_state.record_mode(str(self.repo), "bypassPermissions")
        out = self.profile_cmd(self.repo)
        self.assertIn("--permission-mode bypassPermissions", out)

    def test_modes_are_independent_per_profile_in_the_same_repo(self):
        permission_state.record_launch(str(self.repo), "cmd-a", "auto", profile="alpha")
        permission_state.record_launch(str(self.repo), "cmd-b", "plan", profile="beta")
        data = permission_state.read()
        self.assertEqual(data[str(self.repo)]["alpha"]["mode"], "auto")
        self.assertEqual(data[str(self.repo)]["beta"]["mode"], "plan")
        self.assertEqual(permission_state.mode_for(str(self.repo), "cmd-a", True, "alpha"), "auto")
        self.assertEqual(permission_state.mode_for(str(self.repo), "cmd-b", True, "beta"), "plan")

    def test_old_format_is_read_as_the_claude_profile(self):
        (self.state / "permission.json").write_text(json.dumps({
            str(self.repo): {"command": self.command, "mode": "plan"}}), encoding="utf-8")
        self.assertEqual(permission_state.mode_for(str(self.repo), self.command, True), "plan")
        self.assertEqual(permission_state.mode_for(str(self.repo), self.command, True, "claude"),
                         "plan")

    def test_record_mode_preserves_the_command_and_updates_only_the_mode(self):
        permission_state.record_launch(str(self.repo), self.command, "acceptEdits", profile="claude")
        permission_state.record_mode(str(self.repo), "plan", profile="claude")
        entry = permission_state.read()[str(self.repo)]["claude"]
        self.assertEqual(entry["command"], self.command)
        self.assertEqual(entry["mode"], "plan")


class HookPermissionModeTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name).resolve()
        self.state = self.root / "state"
        self.repo = self.root / "repo"
        self.state.mkdir()
        self.repo.mkdir()
        self.config = self.root / "config.json"
        self.config.write_text('{"notifications": false}', encoding="utf-8")
        env = mock.patch.dict(os.environ, {"WS_STATE": str(self.state)})
        env.start()
        self.addCleanup(env.stop)

    def run_hook(self, payload, **extra):
        env = dict(os.environ, WS_STATE=str(self.state), AT_WS_CONFIG=str(self.config), **extra)
        return subprocess.run([sys.executable, str(SRC / "hook.py")], input=json.dumps(payload),
                              text=True, capture_output=True, env=env, timeout=60)

    def test_stop_records_the_last_permission_mode(self):
        transcript = self.root / "transcript.jsonl"
        transcript.write_text("\n".join([
            json.dumps({"type": "permission-mode", "permissionMode": "acceptEdits"}),
            json.dumps({"type": "permission-mode", "permissionMode": "bypassPermissions"}),
        ]) + "\n", encoding="utf-8")
        proc = self.run_hook({"hook_event_name": "Stop", "session_id": "sess-1",
                              "cwd": str(self.repo), "transcript_path": str(transcript)})
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(permission_state.read()[str(self.repo)]["claude"]["mode"], "bypassPermissions")

    def test_session_end_records_the_last_permission_mode(self):
        transcript = self.root / "transcript.jsonl"
        transcript.write_text(json.dumps(
            {"type": "permission-mode", "permissionMode": "plan"}) + "\n", encoding="utf-8")
        proc = self.run_hook({"hook_event_name": "SessionEnd", "session_id": "sess-2",
                              "cwd": str(self.repo), "transcript_path": str(transcript)})
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(permission_state.read()[str(self.repo)]["claude"]["mode"], "plan")

    def test_hook_records_the_mode_under_ws_session_profile(self):
        transcript = self.root / "transcript.jsonl"
        transcript.write_text(json.dumps(
            {"type": "permission-mode", "permissionMode": "plan"}) + "\n", encoding="utf-8")
        proc = self.run_hook({"hook_event_name": "Stop", "session_id": "sess-3",
                              "cwd": str(self.repo), "transcript_path": str(transcript)},
                             WS_SESSION_PROFILE="deepseek")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(permission_state.read()[str(self.repo)]["deepseek"]["mode"], "plan")


if __name__ == "__main__":
    unittest.main()
