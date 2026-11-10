"""Tests for the per-profile session command (item 97): every profile opens with `--continue ||` and
falls back to a fresh session; the workspace-generated settings and permission mode only apply to
managed Claude profiles; the OpenCode harness carries only its own config; the recorded identity is
the base line, without the session box. Each test runs `src/lib.py` in a child process with
temporary HOME, WS_STATE and AT_WS_CONFIG."""
import json
import os
import shlex
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LIB = ROOT / "src" / "lib.py"


class SessionCommandTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name).resolve()
        self.home = self.root / "home"
        self.state = self.root / "state"
        self.home.mkdir()
        self.state.mkdir()
        self.config = self.root / "config.json"
        self.repo = self.root / "repo"
        self.repo.mkdir()

    def configure(self, config):
        self.config.write_text(json.dumps(config), encoding="utf-8")

    def env(self):
        return dict(os.environ, HOME=str(self.home), WS_STATE=str(self.state),
                    AT_WS_CONFIG=str(self.config),
                    AT_HARNESS_DIR=str(self.root / "no-harness"))

    def run_lib(self, *args):
        proc = subprocess.run([sys.executable, str(LIB), *args], env=self.env(),
                              capture_output=True, text=True, check=False)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return proc.stdout.strip()

    def box(self, profile, repo=None):
        root = str(Path(repo or self.repo).resolve())
        return f"env WS_SESSION_ROLE=repo WS_SESSION_ROOT={root} WS_SESSION_PROFILE={profile}"

    def claude(self, profile="claude", command="claude", **config):
        cfg = {"agent": profile, "profiles": {profile: command}}
        cfg.update(config)
        self.configure(cfg)

    def opencode(self, profile="oc", command="opencode", config="~/oc.json"):
        entry = {"harness": "opencode", "command": command}
        if config:
            entry["config"] = config
        self.configure({"agent": profile, "profiles": {profile: entry}})

    # --- Claude harness ----------------------------------------------------------------------
    def test_managed_claude_session_command_has_settings_profile_and_continue(self):
        self.claude()
        out = self.run_lib("session-command", "claude", str(self.repo))
        base = (f"{self.box('claude')} claude --settings "
                f"{shlex.quote(str(self.state / 'settings' / 'claude.json'))} "
                f"--permission-mode acceptEdits")
        self.assertEqual(out, f"{base} --continue || {base}")
        second = out.split("||", 1)[1]
        self.assertNotIn("--continue", second)

    def test_mapping_claude_profile_carries_its_profile_in_the_box(self):
        self.configure({"agent": "deepseek", "profiles": {
            "claude": "claude", "deepseek": {"command": "claude"}}})
        out = self.run_lib("session-command", "deepseek", str(self.repo))
        self.assertIn("WS_SESSION_PROFILE=deepseek", out)
        self.assertIn("--permission-mode acceptEdits", out)
        self.assertIn("--continue ||", out)

    def test_unmanaged_claude_session_command_has_continue_without_claude_flags(self):
        self.claude(manage=False)
        out = self.run_lib("session-command", "claude", str(self.repo))
        self.assertEqual(out, f"{self.box('claude')} claude --continue || "
                              f"{self.box('claude')} claude")
        for flag in ("--settings", "--permission-mode", "ENABLE_TOOL_SEARCH"):
            self.assertNotIn(flag, out)

    def test_profile_cmd_has_no_continue(self):
        self.claude()
        out = self.run_lib("profile-cmd", "claude", str(self.repo))
        self.assertNotIn("--continue", out)
        self.assertIn("--permission-mode acceptEdits", out)

    # --- OpenCode harness --------------------------------------------------------------------
    def test_opencode_session_command_has_config_and_continue_without_claude_flags(self):
        self.opencode()
        out = self.run_lib("session-command", "oc", str(self.repo))
        config = str(self.home / "oc.json")
        base = f"{self.box('oc')} env OPENCODE_CONFIG={shlex.quote(config)} opencode"
        self.assertEqual(out, f"{base} --continue || {base}")
        for flag in ("--settings", "--permission-mode", "ENABLE_TOOL_SEARCH"):
            self.assertNotIn(flag, out)

    def test_opencode_without_config_has_continue_and_no_claude_flag(self):
        self.opencode(config="")
        out = self.run_lib("session-command", "oc", str(self.repo))
        self.assertEqual(out, f"{self.box('oc')} opencode --continue || {self.box('oc')} opencode")

    # --- opening-command delegates to the default profile ------------------------------------
    def test_opening_command_is_the_default_profile_session_command(self):
        self.claude()
        self.assertEqual(self.run_lib("opening-command", str(self.repo)),
                         self.run_lib("session-command", "claude", str(self.repo)))

    # --- recorded identity -------------------------------------------------------------------
    def test_recorded_identity_is_the_base_line_without_the_session_box(self):
        self.opencode()
        self.run_lib("session-command", "oc", str(self.repo))
        saved = json.loads((self.state / "agent.json").read_text(encoding="utf-8"))
        self.assertEqual(saved[str(self.repo.resolve())]["command"], "opencode [oc]")
        self.assertNotIn("WS_SESSION_ROLE", json.dumps(saved))
        self.assertNotIn("WS_SESSION_PROFILE", json.dumps(saved))


if __name__ == "__main__":
    unittest.main()
