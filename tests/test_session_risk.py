"""Tests for the DeepSeek risk warning (item 101): session_risk.risky, its --check/--confirm CLI
and the risk_check call in bin/workspace.

Every test uses a temporary HOME, WS_STATE and AT_WS_CONFIG, so the real config and cache are never
read or written. The CLI is exercised as a child process, the same way bin/workspace calls it."""
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
SESSION_RISK = SRC / "session_risk.py"
WORKSPACE = ROOT / "bin" / "workspace"

sys.path.insert(0, str(SRC))

import session_risk  # noqa: E402

CONFIG = {
    "agent": "claude",
    "profiles": {
        "claude": "claude",
        "claude-deepseek": {
            "command": "claude",
            "settings": {"env": {"ANTHROPIC_BASE_URL": "https://api.deepseek.com/anthropic"}},
        },
        "custom": {
            "command": "claude",
            "settings": {"env": {"ANTHROPIC_BASE_URL": "https://api.deepseek.com/v1"}},
        },
        "claude-normal": {
            "command": "claude",
            "settings": {"env": {"ANTHROPIC_BASE_URL": "https://api.anthropic.com"}},
        },
        "opencode-deepseek": {"harness": "opencode", "command": "opencode", "config": "/tmp/oc.json"},
    },
}


class SessionRiskTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name).resolve()
        self.home = self.root / "home"
        self.state = self.root / "state"
        self.home.mkdir()
        self.state.mkdir()
        self.config = self.root / "config.json"
        self.config.write_text(json.dumps(CONFIG), encoding="utf-8")
        self._env = mock.patch.dict(os.environ, {
            "HOME": str(self.home),
            "WS_STATE": str(self.state),
            "AT_WS_CONFIG": str(self.config),
        })
        self._env.start()
        self.addCleanup(self._env.stop)

    def run_cli(self, *args, input=None):
        return subprocess.run(
            [sys.executable, str(SESSION_RISK), *args],
            env=dict(os.environ), input=input, capture_output=True, text=True, check=False,
        )

    # risky ---------------------------------------------------------------------------------
    def test_risky_deepseek_name(self):
        self.assertTrue(session_risk.risky("claude-deepseek"))

    def test_risky_deepseek_base_url(self):
        self.assertTrue(session_risk.risky("custom"))

    def test_risky_normal_claude(self):
        self.assertFalse(session_risk.risky("claude-normal"))

    def test_risky_opencode(self):
        self.assertFalse(session_risk.risky("opencode-deepseek"))

    def test_risky_claude(self):
        self.assertFalse(session_risk.risky("claude"))

    def test_risky_unknown(self):
        self.assertFalse(session_risk.risky("desconhecido"))

    # --check -------------------------------------------------------------------------------
    def test_check_deepseek_exit0(self):
        self.assertEqual(self.run_cli("--check", "claude-deepseek").returncode, 0)

    def test_check_normal_claude_exit1(self):
        self.assertEqual(self.run_cli("--check", "claude-normal").returncode, 1)

    def test_check_opencode_exit1(self):
        self.assertEqual(self.run_cli("--check", "opencode-deepseek").returncode, 1)

    # --confirm -----------------------------------------------------------------------------
    def confirm_status(self, data):
        status = self.root / "status.txt"
        proc = self.run_cli("--confirm", "claude-deepseek", "--status", str(status), input=data)
        return proc, status

    def test_confirm_yes(self):
        proc, status = self.confirm_status("s\n")
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(status.read_text(encoding="utf-8"), "yes")

    def test_confirm_no(self):
        proc, status = self.confirm_status("n\n")
        self.assertEqual(proc.returncode, 1)
        self.assertEqual(status.read_text(encoding="utf-8"), "no")

    def test_confirm_esc(self):
        proc, status = self.confirm_status("\x1b")
        self.assertEqual(proc.returncode, 1)
        self.assertEqual(status.read_text(encoding="utf-8"), "no")

    # integration ---------------------------------------------------------------------------
    def test_workspace_calls_risk_check_before_creating_the_session(self):
        text = WORKSPACE.read_text(encoding="utf-8")
        self.assertIn("session_risk.py", text)
        self.assertIn("--check", text)
        self.assertIn("--confirm", text)
        start = text.index("session_new() {")
        end = text.index("session_run()", start)
        body = text[start:end]
        self.assertIn('risk_check "$W" "$profile" "$path"', body)
        self.assertLess(body.index('risk_check "$W"'), body.index('tab_list "$W" s'))


if __name__ == "__main__":
    unittest.main()
