"""Tests for the OpenCode plugin recording the session id per tab (item 102c).

With `node` available, the plugin is loaded and `recordSession` runs against a temporary WS_STATE.
Without it, a source check pins the same strings; the module is not loaded in that case."""
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PLUGIN = ROOT / "harness" / "opencode-policy.js"
NODE = shutil.which("node")

NODE_SCRIPT = """
const plugin = require(process.argv[1]);
plugin.recordSession(
  { sessionID: process.argv[3] },
  { WS_STATE: process.argv[2], WS_SESSION_TAB: process.argv[4] }
);
"""


class OpenCodeSessionTest(unittest.TestCase):
    def test_plugin_records_the_session_id_for_the_tab(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / "state"
            state.mkdir()
            if NODE:
                proc = subprocess.run(
                    [NODE, "-e", NODE_SCRIPT, str(PLUGIN), str(state), "sess-1", "tab-1"],
                    capture_output=True, text=True, timeout=60)
                self.assertEqual(proc.returncode, 0, proc.stderr)
                self.assertEqual((state / "tabs" / "session-tab-1").read_text(), "sess-1")
            else:
                source = PLUGIN.read_text(encoding="utf-8")
                for needle in ("WS_SESSION_TAB", "session-", "recordSession"):
                    self.assertIn(needle, source)


if __name__ == "__main__":
    unittest.main()
