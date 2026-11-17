"""A click on an unfocused tmux pane only focuses it; the app inside never gets that press.

The binding lives in config/tmux.conf (`MouseDown1Pane`). The live test reads that real line, starts a
private tmux server with two panes, attaches a client through a pty and sends SGR mouse events the way a
terminal does. A recorder app in each pane logs the bytes it receives, so the test sees what the app sees:
a click on the unfocused pane focuses it and sends no press; the next click reaches the app.
The test is skipped when tmux is not installed.
"""
import fcntl
import os
import pty
import re
import shutil
import signal
import struct
import subprocess
import tempfile
import termios
import threading
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONF = ROOT / "config" / "tmux.conf"

# A pane app that turns on mouse reporting (1000 + 1006), then logs every byte it reads from its tty.
RECORDER = """
import os, sys, tty
log = sys.argv[1]
sys.stdout.write("\\x1b[?1000h\\x1b[?1006h")
sys.stdout.flush()
tty.setraw(0)
with open(log, "ab", buffering=0) as f:
    while True:
        data = os.read(0, 1024)
        if not data:
            break
        f.write(data)
"""

PRESS = re.compile(rb"\x1b\[<0;\d+;\d+M")


def binding_line(conf):
    for line in conf.splitlines():
        if line.startswith("bind -n MouseDown1Pane"):
            return line
    raise AssertionError("config/tmux.conf has no `bind -n MouseDown1Pane`")


class ClickBindingTextTest(unittest.TestCase):
    def test_binding_focuses_only_when_the_pane_is_not_active(self):
        line = binding_line(CONF.read_text(encoding="utf-8"))
        self.assertTrue(line.endswith(
            'if-shell -F -t = "#{pane_active}" { send-keys -t = -M } { select-pane -t = }'), line)


@unittest.skipUnless(shutil.which("tmux"), "tmux is not installed")
class ClickFocusLiveTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ws-click-")
        self.socket = f"ws-click-{os.getpid()}-{time.monotonic_ns()}"
        self.recorder = Path(self.tmp) / "recorder.py"
        self.recorder.write_text(RECORDER, encoding="utf-8")
        self.log = {"left": Path(self.tmp) / "left.log", "right": Path(self.tmp) / "right.log"}
        conf = Path(self.tmp) / "tmux.conf"
        conf.write_text("set -g mouse on\n" + binding_line(CONF.read_text(encoding="utf-8")) + "\n",
                        encoding="utf-8")
        self.conf = conf
        self.child = None

    def tmux(self, *args):
        return subprocess.run(["tmux", "-L", self.socket, *args], capture_output=True, text=True,
                              check=False)

    def show(self, fmt, target):
        return self.tmux("display", "-p", "-t", target, fmt).stdout.strip()

    def wait(self, predicate, what, timeout=5.0):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if predicate():
                return
            time.sleep(0.05)
        self.fail(f"timed out waiting for {what}")

    def start(self):
        python = shutil.which("python3") or "python3"
        left = f"{python} {self.recorder} {self.log['left']}"
        right = f"{python} {self.recorder} {self.log['right']}"
        started = self.tmux("-f", str(self.conf), "new-session", "-d", "-s", "t", "-x", "120", "-y", "30",
                            left)
        self.assertEqual(started.returncode, 0, started.stderr)
        split = self.tmux("split-window", "-h", "-t", "t:0", right)
        self.assertEqual(split.returncode, 0, split.stderr)
        self.wait(lambda: self.show("#{mouse_any_flag}", "t:0.0") == "1", "left pane mouse mode")
        self.wait(lambda: self.show("#{mouse_any_flag}", "t:0.1") == "1", "right pane mouse mode")
        self.attach()
        self.wait(lambda: self.tmux("list-clients", "-t", "t").stdout.strip() != "", "a client")

    def attach(self):
        pid, fd = pty.fork()
        if pid == 0:     # child: the tmux client, on the pty
            fcntl.ioctl(0, termios.TIOCSWINSZ, struct.pack("HHHH", 30, 120, 0, 0))
            # The client needs a real terminal type: with TERM=dumb (CI) tmux refuses to attach.
            # Only the child's environment changes; the test process keeps its own TERM.
            os.execvpe("tmux", ["tmux", "-L", self.socket, "attach", "-t", "t"],
                       dict(os.environ, TERM="xterm-256color"))
        self.child = (pid, fd)
        threading.Thread(target=self.drain, args=(fd,), daemon=True).start()

    @staticmethod
    def drain(fd):
        while True:
            try:
                if not os.read(fd, 4096):
                    return
            except OSError:
                return

    def click(self, x, y):
        """Press and release at 1-based column x, row y, the way a terminal reports it."""
        _, fd = self.child
        os.write(fd, f"\x1b[<0;{x};{y}M\x1b[<0;{x};{y}m".encode())

    def presses(self, name):
        path = self.log[name]
        return PRESS.findall(path.read_bytes()) if path.exists() else []

    def test_click_on_unfocused_pane_only_focuses_then_next_click_acts(self):
        try:
            self.start()
            self.assertEqual(self.show("#{pane_active}", "t:0.0"), "0", "the right pane starts active")

            self.click(10, 5)            # the left pane is not focused
            self.wait(lambda: self.show("#{pane_active}", "t:0.0") == "1", "the left pane to get focus")
            time.sleep(0.5)
            self.assertEqual(self.presses("left"), [], "the focusing click must not reach the app")

            self.click(10, 5)            # the left pane is focused now
            self.wait(lambda: len(self.presses("left")) == 1, "the second click to reach the app")
            self.assertEqual(self.presses("right"), [])
        finally:
            self.cleanup()

    def cleanup(self):
        self.tmux("kill-server")
        if self.child:
            pid, fd = self.child
            try:
                os.close(fd)
            except OSError:
                pass
            try:
                os.kill(pid, signal.SIGKILL)
                os.waitpid(pid, 0)
            except (OSError, ChildProcessError):
                pass
        shutil.rmtree(self.tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
