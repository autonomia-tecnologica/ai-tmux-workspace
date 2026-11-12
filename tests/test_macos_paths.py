"""macOS path and process detection: Homebrew PATH entries, `ps` fallbacks and a case- and
normalization-insensitive repo identity.

Darwin is always simulated by patching `_is_darwin`, and every `ps` call is a canned
`subprocess.run`, so no test shells out to the real one (which would not exist on Linux anyway)."""
import os
import sys
import tempfile
import unicodedata
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
sys.path.insert(0, str(SRC))

import hook  # noqa: E402
import lib  # noqa: E402
import session_state  # noqa: E402


def fake_ps(answers):
    """A `subprocess.run` replacement that answers `ps -o <field>= -p <pid>` from a dict.

    A missing field stands for `ps` failing (no such process): returncode 1 and no output."""
    def run(argv, **_kwargs):
        field = argv[2].rstrip("=")
        value = answers.get(field)
        if value is None:
            return mock.Mock(returncode=1, stdout="", stderr="ps: no such process")
        return mock.Mock(returncode=0, stdout=f"{value}\n", stderr="")
    return run


class AgentPathTest(unittest.TestCase):
    def test_agent_path_adds_homebrew_folders(self):
        with mock.patch.object(lib, "_is_darwin", return_value=True), \
                mock.patch.object(lib.os.path, "isdir", return_value=True):
            entries = lib.agent_path(path="/usr/bin").split(os.pathsep)
        for folder in ("/opt/homebrew/bin", "/opt/homebrew/sbin",
                       "/usr/local/bin", "/usr/local/sbin"):
            self.assertIn(folder, entries)
        self.assertEqual(len(entries), len({os.path.normpath(e) for e in entries}))

    def test_existing_homebrew_folder_is_not_duplicated(self):
        with mock.patch.object(lib, "_is_darwin", return_value=True), \
                mock.patch.object(lib.os.path, "isdir", return_value=True):
            entries = lib.agent_path(path="/opt/homebrew/bin:/usr/bin").split(os.pathsep)
        self.assertEqual(entries.count("/opt/homebrew/bin"), 1)

    def test_agent_path_leaves_the_linux_path_alone(self):
        with mock.patch.object(lib, "_is_darwin", return_value=False), \
                mock.patch.object(lib.os.path, "isdir", return_value=True):
            entries = lib.agent_path(path="/usr/bin").split(os.pathsep)
        self.assertNotIn("/opt/homebrew/bin", entries)
        self.assertNotIn("/usr/local/bin", entries)


class DarwinPidAliveTest(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.object(lib, "_is_darwin", return_value=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_rejects_a_zombie(self):
        ps = fake_ps({"stat": "Z+", "command": "node claude-code/cli.js"})
        with mock.patch.object(lib.subprocess, "run", side_effect=ps):
            self.assertFalse(lib._pid_alive(os.getpid()))

    def test_confirms_a_live_agent(self):
        ps = fake_ps({"stat": "S+", "command": "node /usr/local/lib/claude-code/cli.js"})
        with mock.patch.object(lib.subprocess, "run", side_effect=ps):
            self.assertTrue(lib._pid_alive(os.getpid()))

    def test_rejects_a_reused_pid(self):
        ps = fake_ps({"stat": "S", "command": "python3 worker.py"})
        with mock.patch.object(lib.subprocess, "run", side_effect=ps):
            self.assertFalse(lib._pid_alive(os.getpid()))

    def test_a_failing_ps_is_not_alive(self):
        with mock.patch.object(lib.subprocess, "run", side_effect=fake_ps({})):
            self.assertFalse(lib._pid_alive(os.getpid()))


class DarwinHookProcessTest(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.object(hook, "_is_darwin", return_value=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_proc_stat_parses_ps(self):
        ps = fake_ps({"ppid": "42", "comm": "/opt/homebrew/bin/claude"})
        with mock.patch.object(hook.subprocess, "run", side_effect=ps):
            self.assertEqual(hook._proc_stat(1234), (42, "claude"))

    def test_is_claude_matches_the_node_cli(self):
        with mock.patch.object(hook.subprocess, "run",
                               side_effect=fake_ps({"command": "node /usr/local/lib/"
                                                               "claude-code/cli.js"})):
            self.assertTrue(hook._is_claude(1234, "node"))

    def test_is_not_claude_for_another_command(self):
        with mock.patch.object(hook.subprocess, "run",
                               side_effect=fake_ps({"command": "python3 worker.py"})):
            self.assertFalse(hook._is_claude(1234, "python3"))

    def test_claude_pid_walks_up_through_ps(self):
        ps = fake_ps({"ppid": "1", "comm": "claude"})
        with mock.patch.object(hook.os, "getppid", return_value=987), \
                mock.patch.object(hook.subprocess, "run", side_effect=ps):
            self.assertEqual(hook._claude_pid(), 987)

    def test_claude_pid_fails_safe_when_ps_fails(self):
        with mock.patch.object(hook.os, "getppid", return_value=987), \
                mock.patch.object(hook.subprocess, "run", side_effect=fake_ps({})):
            self.assertEqual(hook._claude_pid(), 0)


class RepoIdentityTest(unittest.TestCase):
    def test_darwin_repo_of_ignores_case(self):
        with mock.patch.object(session_state, "_is_darwin", return_value=True):
            self.assertEqual(lib.repo_of("/Users/me/Repo/src", ["/Users/me/repo"]),
                             "/Users/me/repo")
            # APFS is also normalization-insensitive: NFC and NFD spell one directory.
            nfc = unicodedata.normalize("NFC", "/Users/me/caf\u00e9")
            nfd = unicodedata.normalize("NFD", nfc)
            self.assertNotEqual(nfc, nfd)
            self.assertEqual(lib.repo_of(f"{nfc}/src", [nfd]), nfd)

    def test_linux_repo_of_is_byte_exact(self):
        with mock.patch.object(session_state, "_is_darwin", return_value=False):
            self.assertIsNone(lib.repo_of("/Users/me/Repo/src", ["/Users/me/repo"]))
            self.assertIsNone(lib.repo_of("/Users/me/repo2/src", ["/Users/me/repo"]))
            self.assertEqual(lib.repo_of("/Users/me/repo/src", ["/Users/me/repo"]),
                             "/Users/me/repo")

    def test_darwin_repo_of_keeps_the_longest_match(self):
        with mock.patch.object(session_state, "_is_darwin", return_value=True):
            paths = ["/Users/me/repo", "/Users/me/repo/work"]
            self.assertEqual(lib.repo_of("/Users/me/Repo/Work/a", paths),
                             "/Users/me/repo/work")


class SessionStateKeyTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name).resolve()
        self.state = self.root / "state"
        self.state.mkdir()
        env = mock.patch.dict(os.environ, {"WS_STATE": str(self.state)})
        env.start()
        self.addCleanup(env.stop)

    def test_darwin_key_ignores_case(self):
        with mock.patch.object(session_state, "_is_darwin", return_value=True):
            self.assertEqual(session_state.state_path(self.root / "Repo"),
                             session_state.state_path(self.root / "repo"))

    def test_darwin_key_ignores_normalization(self):
        nfc = unicodedata.normalize("NFC", str(self.root / "caf\u00e9"))
        nfd = unicodedata.normalize("NFD", nfc)
        with mock.patch.object(session_state, "_is_darwin", return_value=True):
            self.assertEqual(session_state.state_path(nfc), session_state.state_path(nfd))

    def test_linux_key_is_byte_exact(self):
        with mock.patch.object(session_state, "_is_darwin", return_value=False):
            self.assertNotEqual(session_state.state_path(self.root / "Repo"),
                                session_state.state_path(self.root / "repo"))
            nfc = unicodedata.normalize("NFC", str(self.root / "caf\u00e9"))
            nfd = unicodedata.normalize("NFD", nfc)
            self.assertNotEqual(session_state.state_path(nfc),
                                session_state.state_path(nfd))

    def test_displayed_path_keeps_the_written_form(self):
        with mock.patch.object(session_state, "_is_darwin", return_value=True):
            self.assertEqual(session_state._empty(self.root / "Repo")["repo"],
                             os.path.realpath(str(self.root / "Repo")))


if __name__ == "__main__":
    unittest.main()
