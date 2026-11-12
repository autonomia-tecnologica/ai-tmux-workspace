"""Lifecycle owner: the `ws-*` server <-> profile mapping, the live servers, and the bash dispatch.

`src/lifecycle.py` names the servers and their profiles; `bin/workspace` (stop, restart, reload) acts
on each one. The bash tests run the real `bin/workspace` against a fake `tmux` on PATH that logs every
call and keeps the live servers in a file, so the dispatch is proven without a tmux server.
"""
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
sys.path.insert(0, str(SRC))

import lifecycle  # noqa: E402

FAKE_TMUX = """#!/usr/bin/env bash
# Fake tmux: logs the call; live servers are lines "<socket> <session>" in $FAKE_LIVE.
echo "$*" >> "$FAKE_LOG"
server=""
if [ "${1:-}" = "-L" ]; then server="$2"; shift 2; fi
case "$1" in
  has-session) awk -v s="$server" '$1==s{f=1} END{exit !f}' "$FAKE_LIVE" ;;
  list-sessions) awk -v s="$server" '$1==s{print $2}' "$FAKE_LIVE" ;;
  kill-server) awk -v s="$server" '$1!=s' "$FAKE_LIVE" > "$FAKE_LIVE.tmp" && mv "$FAKE_LIVE.tmp" "$FAKE_LIVE" ;;
  display) echo "/tmp/fake-tmux.sock,1,0" ;;
  list-windows) echo "x" ;;
esac
exit 0
"""


class NamingTests(unittest.TestCase):
    def test_server_and_profile_are_the_same_name_with_a_prefix(self):
        self.assertEqual(lifecycle.server_of("claude"), "ws-claude")
        self.assertEqual(lifecycle.profile_of("ws-opencode-claude"), "opencode-claude")

    def test_a_name_without_the_prefix_is_not_a_workspace_server(self):
        for name in ("default", "tmux-1000", "ws-", ""):
            with self.subTest(name=name):
                self.assertEqual(lifecycle.profile_of(name), "")

    def test_argument_is_a_profile_or_its_server(self):
        self.assertEqual(lifecycle.profile_of_argument("ws-claude"), "claude")
        self.assertEqual(lifecycle.profile_of_argument("claude"), "claude")
        self.assertEqual(lifecycle.profiles("ws-opencode-claude"), ["opencode-claude"])
        self.assertEqual(lifecycle.profiles("opencode-claude"), ["opencode-claude"])

    def test_an_argument_that_names_no_profile_selects_nothing(self):
        self.assertEqual(lifecycle.profiles("ws-"), [])


class LiveServerTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.mkdtemp(prefix="lc-")
        self.addCleanup(shutil.rmtree, self.folder, True)
        self.sockets = []
        for name in ("ws-claude", "ws-opencode-claude", "ws-dead", "other"):
            self.sockets.append(self._socket(name))
        open(os.path.join(self.folder, "ws-plain-file"), "w").close()
        patcher = mock.patch.object(lifecycle, "socket_dir", return_value=self.folder)
        patcher.start()
        self.addCleanup(patcher.stop)
        sessions = {"ws-claude": {"claude"}, "ws-opencode-claude": {"opencode-claude"}}
        patcher = mock.patch.object(lifecycle, "_sessions",
                                    side_effect=lambda server: sessions.get(server, set()))
        patcher.start()
        self.addCleanup(patcher.stop)

    def _socket(self, name):
        path = os.path.join(self.folder, name)
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.bind(path)
        sock.close()
        self.addCleanup(lambda: os.path.exists(path) and os.remove(path))
        return path

    def test_live_profiles_are_the_workspace_sockets_with_a_session(self):
        # ws-dead has a socket but no session; "other" and the plain file are not workspace servers.
        self.assertEqual(lifecycle.live_profiles(), ["claude", "opencode-claude"])

    def test_no_argument_acts_on_every_live_server(self):
        self.assertEqual(lifecycle.profiles(), ["claude", "opencode-claude"])

    def test_missing_socket_folder_has_no_live_server(self):
        with mock.patch.object(lifecycle, "socket_dir", return_value=self.folder + "-absent"):
            self.assertEqual(lifecycle.live_profiles(), [])


class WorkspaceDispatchTests(unittest.TestCase):
    """Runs the real bin/workspace with a fake tmux: stop, restart and reload act per live server."""

    def setUp(self):
        self.home = tempfile.mkdtemp(prefix="lcw-")
        self.addCleanup(shutil.rmtree, self.home, True)
        self.fake = Path(self.home) / "bin"
        self.fake.mkdir()
        tmux = self.fake / "tmux"
        tmux.write_text(FAKE_TMUX, encoding="utf-8")
        tmux.chmod(0o755)
        self.log = Path(self.home) / "calls.log"
        self.live = Path(self.home) / "live.txt"
        # ws-dead has a socket but no session: it is not live, so no command may touch it.
        self.live.write_text("ws-claude claude\nws-opencode-claude opencode-claude\n", encoding="utf-8")
        self.sockets = Path(self.home) / "sockets" / f"tmux-{os.getuid()}"
        self.sockets.mkdir(parents=True)
        for name in ("ws-claude", "ws-opencode-claude", "ws-dead"):
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            sock.bind(str(self.sockets / name))
            sock.close()
        self.addCleanup(shutil.rmtree, str(self.sockets.parent), True)

    def run_workspace(self, *args):
        env = {
            "PATH": f"{self.fake}{os.pathsep}{os.environ.get('PATH', '')}",
            "HOME": self.home,
            "TMUX_TMPDIR": str(Path(self.home) / "sockets"),
            "WS_STATE": str(Path(self.home) / "state"),
            "WS_NO_ATTACH": "1",
            "FAKE_LOG": str(self.log),
            "FAKE_LIVE": str(self.live),
            "LANG": "C.UTF-8",
        }
        return subprocess.run(["bash", str(ROOT / "bin" / "workspace"), *args], env=env,
                              capture_output=True, text=True, timeout=60)

    def calls(self):
        return self.log.read_text(encoding="utf-8").splitlines() if self.log.exists() else []

    def killed(self):
        return [c.split()[1] for c in self.calls() if c.endswith("kill-server")]

    def test_stop_without_argument_kills_every_live_server_and_no_other(self):
        result = self.run_workspace("stop")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.killed(), ["ws-claude", "ws-opencode-claude"])

    def test_stop_with_a_server_name_kills_only_that_server(self):
        result = self.run_workspace("stop", "ws-opencode-claude")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.killed(), ["ws-opencode-claude"])

    def test_stop_with_a_profile_name_kills_only_that_profile(self):
        result = self.run_workspace("stop", "claude")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.killed(), ["ws-claude"])

    def test_an_argument_that_names_no_profile_is_a_usage_error(self):
        result = self.run_workspace("stop", "ws-")
        self.assertEqual(result.returncode, 2)
        self.assertEqual(self.killed(), [])

    def test_restart_without_argument_restarts_each_live_server(self):
        result = self.run_workspace("restart")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.killed(), ["ws-claude", "ws-opencode-claude"])

    def test_reload_without_argument_reloads_each_live_server(self):
        result = self.run_workspace("reload")
        self.assertEqual(result.returncode, 0, result.stderr)
        checked = sorted({c.split()[1] for c in self.calls()
                          if c.startswith("-L ") and c.endswith("has-session -t " + c.split()[1][3:])})
        self.assertEqual(checked, ["ws-claude", "ws-opencode-claude"])

    def test_no_live_server_keeps_the_default_profile(self):
        self.live.write_text("", encoding="utf-8")
        result = self.run_workspace("stop")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.killed(), ["ws-claude"])


class ResumeIsGoneTests(unittest.TestCase):
    def test_the_dead_resume_command_is_not_in_the_script(self):
        text = (ROOT / "bin" / "workspace").read_text(encoding="utf-8")
        self.assertNotIn("\nresume() {", text)
        self.assertNotIn("  resume) shift", text)
        self.assertNotIn("profile_for_window", text)

    def test_restart_and_stop_share_one_save_and_kill_block(self):
        text = (ROOT / "bin" / "workspace").read_text(encoding="utf-8")
        self.assertEqual(text.count("state_sync_profile \"$profile\""), 1)
        self.assertEqual(text.count("kill-server"), 1)


if __name__ == "__main__":
    unittest.main()
