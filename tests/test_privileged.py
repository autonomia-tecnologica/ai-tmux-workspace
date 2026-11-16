"""Tests for the administrative approval (items 154 and 159) with the desktop window and pkexec stubbed.

The window tool (zenity, kdialog) and pkexec are shell stubs on a PATH the test controls, so no real
window opens and no password is asked. The stubs record their arguments; the test answers through
STUB_ANSWER (yes, no, sleep) and the exit code of pkexec through STUB_CODE.
"""
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

import i18n  # noqa: E402
import privileged  # noqa: E402

ZENITY_STUB = """#!/bin/sh
printf '%s\\0' "$@" > "$STUB_ZLOG"
case "$STUB_ANSWER" in
  yes) exit 0 ;;
  sleep) sleep 5; exit 0 ;;
  *) exit 1 ;;
esac
"""

KDIALOG_STUB = """#!/bin/sh
printf '%s\\0' "$@" > "$STUB_ZLOG"
exit 1
"""

PKEXEC_STUB = """#!/bin/sh
printf '%s\\0' "$@" >> "$STUB_PKLOG"
echo "saida: $1"
echo "erro: $DBUS_SESSION_BUS_ADDRESS" >&2
exit "${STUB_CODE:-0}"
"""


def read_args(path):
    """Arguments a stub recorded (NUL-separated), or None when the stub never ran."""
    if not path.exists():
        return None
    return [part for part in path.read_text(encoding="utf-8").split("\0") if part != ""]


class ApprovalTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="ws-test-privileged-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.bin = self.tmp / "bin"
        self.bin.mkdir()
        self.zlog = self.tmp / "window.log"
        self.pklog = self.tmp / "pkexec.log"
        self.write_stub("zenity", ZENITY_STUB)
        self.write_stub("pkexec", PKEXEC_STUB)
        # The stub's `sleep` needs the real sleep; a symlink keeps PATH to the stub folder only, so a
        # real kdialog, zenity or pkexec on the machine can never be found by the test.
        os.symlink(shutil.which("sleep") or "/bin/sleep", self.bin / "sleep")
        self.records = []
        self.env = mock.patch.dict(os.environ, {
            "PATH": str(self.bin),
            "DISPLAY": ":9",
            "DBUS_SESSION_BUS_ADDRESS": "unix:path=/stub-bus",
            "STUB_ZLOG": str(self.zlog),
            "STUB_PKLOG": str(self.pklog),
            "STUB_ANSWER": "no",
            "STUB_CODE": "0",
        })
        self.env.start()
        self.addCleanup(self.env.stop)
        for name in ("TMUX", "WAYLAND_DISPLAY"):
            os.environ.pop(name, None)
        patcher = mock.patch.object(privileged.policy, "record", side_effect=self.records.append)
        patcher.start()
        self.addCleanup(patcher.stop)

    def write_stub(self, name, body):
        path = self.bin / name
        path.write_text(body, encoding="utf-8")
        path.chmod(0o755)

    def run_event(self, command, answer="yes", **env):
        os.environ["STUB_ANSWER"] = answer
        for name, value in env.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = str(value)
        event = {"tool_name": "Bash", "tool_input": {"command": command}, "cwd": str(self.tmp),
                 "session_id": "sess-1"}
        return privileged.run_approved(event)

    def window_text(self):
        args = read_args(self.zlog)
        self.assertIsNotNone(args, "the question window never ran")
        texts = [arg[len("--text="):] for arg in args if arg.startswith("--text=")]
        self.assertEqual(len(texts), 1)
        return texts[0]

    # --- the question ---------------------------------------------------------------------------
    def test_the_window_asks_with_the_whole_command_as_plain_text(self):
        result = self.run_event("sudo ls -la /root", answer="no")
        args = read_args(self.zlog)
        self.assertIn("--no-markup", args)
        self.assertIn("--question", args)
        self.assertEqual(self.window_text(), "Vou rodar este comando administrativo: sudo ls -la /root. Posso?")
        self.assertEqual(result["message"], i18n.t("privileged.refused"))

    def test_the_window_shows_the_full_command_even_when_it_is_long(self):
        long_command = "sudo ls " + " ".join(f"--arg{i}" for i in range(200)) + " --token SEGREDO123"
        self.run_event(long_command, answer="no")
        self.assertIn("SEGREDO123", self.window_text())
        self.assertIn(long_command, self.window_text())

    def test_quotes_substitution_and_markup_stay_literal(self):
        command = 'echo "a" $(touch MARCA) <b>&'
        result = self.run_event(command, answer="yes")
        self.assertIn(command, self.window_text())
        # not a simple command: it runs as `pkexec sh -c <line>`, the line is passed as one argument
        self.assertEqual(read_args(self.pklog), ["sh", "-c", command])
        self.assertFalse((self.tmp / "MARCA").exists())
        self.assertIn("saida: sh", result["message"])

    def test_kdialog_is_preferred_and_gets_the_text_escaped(self):
        self.write_stub("kdialog", KDIALOG_STUB)
        self.assertEqual(privileged.window_tool()[0], "kdialog")
        argv = privileged._window_argv(("kdialog", "/x/kdialog"), 'sudo x <b> & "y"\n$(z)')
        self.assertEqual(argv[1:4], ["--title", privileged.WINDOW_TITLE, "--yesno"])
        self.assertEqual(argv[-1], 'sudo x &lt;b&gt; &amp; &quot;y&quot;<br>$(z)')

    # --- the answer -----------------------------------------------------------------------------
    def test_yes_runs_pkexec_and_returns_the_output_and_the_exit_code(self):
        result = self.run_event("sudo ls -la", answer="yes", STUB_CODE="3")
        self.assertEqual(read_args(self.pklog), ["ls", "-la"])
        self.assertEqual(result["message"], i18n.t("privileged.result", code=3,
                                                   output="saida: ls\nerro: unix:path=/stub-bus"))
        self.assertEqual(self.records[-1]["reason"], "ran")
        self.assertEqual(self.records[-1]["exit"], 3)

    def test_no_answer_runs_nothing_and_the_reason_goes_back(self):
        result = self.run_event("sudo ls", answer="no")
        self.assertIsNone(read_args(self.pklog))
        self.assertEqual(result["message"], i18n.t("privileged.refused"))
        self.assertEqual(self.records[-1]["reason"], "refused")

    def test_closing_the_window_counts_as_no(self):
        self.write_stub("zenity", "#!/bin/sh\nexit 1\n")
        result = self.run_event("sudo ls", answer="yes")
        self.assertIsNone(read_args(self.pklog))
        self.assertEqual(result["message"], i18n.t("privileged.refused"))

    def test_no_answer_in_time_kills_the_window_and_runs_nothing(self):
        with mock.patch.object(privileged, "APPROVAL_SECONDS", 1):
            result = self.run_event("sudo ls", answer="sleep")
        self.assertIsNone(read_args(self.pklog))
        self.assertEqual(result["message"], i18n.t("privileged.timeout"))
        self.assertEqual(self.records[-1]["reason"], "timeout")

    def test_pkexec_refusal_or_missing_program_is_reported_as_refused(self):
        result = self.run_event("sudo ls", answer="yes", STUB_CODE="126")
        self.assertEqual(result["message"], i18n.t("privileged.password", code=126))
        self.assertEqual(self.records[-1]["reason"], "refused")
        self.assertEqual(self.records[-1]["exit"], 126)

    # --- no desktop -----------------------------------------------------------------------------
    def test_without_a_desktop_nothing_runs_and_the_command_goes_back_to_paste(self):
        result = self.run_event("sudo ls -la", answer="yes", DISPLAY=None, WAYLAND_DISPLAY=None)
        self.assertIsNone(read_args(self.zlog))
        self.assertIsNone(read_args(self.pklog))
        self.assertEqual(result["message"], i18n.t("privileged.no_gui", command="sudo ls -la"))
        self.assertEqual(self.records[-1]["reason"], "no_gui")

    def test_wayland_alone_is_a_desktop_too(self):
        self.assertIsNotNone(privileged.window_tool())
        os.environ.pop("DISPLAY", None)
        os.environ["WAYLAND_DISPLAY"] = "wayland-0"
        self.assertEqual(privileged.window_tool()[0], "zenity")

    def test_without_a_window_tool_nothing_runs(self):
        empty = self.tmp / "empty"
        empty.mkdir()
        os.environ["PATH"] = str(empty)
        result = self.run_event("sudo ls", answer="yes")
        self.assertEqual(result["message"], i18n.t("privileged.no_gui", command="sudo ls"))
        self.assertIsNone(read_args(self.pklog))
        self.assertEqual(self.records[-1]["reason"], "no_gui")

    # --- command shape and trace ----------------------------------------------------------------
    def test_sudo_and_doas_become_pkexec_on_the_same_command(self):
        self.assertEqual(privileged.pkexec_argv("sudo ls -la"), ["pkexec", "ls", "-la"])
        self.assertEqual(privileged.pkexec_argv("doas ls"), ["pkexec", "ls"])
        self.assertEqual(privileged.pkexec_argv("ls -la"), ["pkexec", "ls", "-la"])

    def test_the_trace_keeps_the_program_and_a_hash_never_the_arguments(self):
        self.run_event("sudo ls --token SEGREDO123", answer="yes")
        entry = self.records[-1]
        self.assertTrue(entry["target"].startswith("sudo #") or entry["target"].startswith("ls #"),
                        entry["target"])
        self.assertNotIn("SEGREDO123", repr(entry))
        self.assertNotIn("--token", repr(entry))
        self.assertEqual(entry["capability"], "admin")
        self.assertEqual(entry["decision"], "privileged")

    def test_approval_does_not_need_tmux(self):
        self.assertNotIn("TMUX", os.environ)
        result = self.run_event("sudo ls", answer="yes")
        self.assertEqual(self.records[-1]["reason"], "ran")
        self.assertIn("Código de saída", result["message"])

    def test_an_empty_command_runs_nothing(self):
        result = privileged.run_approved({"tool_name": "Bash", "tool_input": {"command": "  "}})
        self.assertEqual(result["message"], i18n.t("privileged.not_shell"))
        self.assertIsNone(read_args(self.zlog))


if __name__ == "__main__":
    unittest.main()
