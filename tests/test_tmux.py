"""Tests for the tmux command-string helpers. tmux.run is mocked, so no tmux server is touched."""
import io
import json
import os
import shlex
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

import i18n  # noqa: E402
import tmux  # noqa: E402


class QuoteCmdTest(unittest.TestCase):
    def test_escapes_backslash_quote_dollar_and_hash(self):
        argv = ["/bin/ws", "send", "/p/a#b", 'say "$HOME" \\ #1']
        # shlex quotes each argument, then tmux escaping: \ -> \\, " -> \", $ -> \$, # -> ##
        expected = '"/bin/ws send \'/p/a##b\' \'say \\"\\$HOME\\" \\\\ ##1\'"'
        self.assertEqual(tmux.quote_cmd(argv), expected)

    def test_plain_argv_only_gets_wrapped(self):
        self.assertEqual(tmux.quote_cmd(["/bin/ws", "send"]), '"/bin/ws send"')

    def test_argument_with_spaces_is_shell_quoted(self):
        self.assertEqual(tmux.quote_cmd(["echo", "a b"]), "\"echo 'a b'\"")


class ConfirmThenTest(unittest.TestCase):
    def test_runs_confirm_before_with_escaped_prompt_and_command(self):
        argv = ["/bin/ws", "send", "/p/a#b", 'say "$HOME"']
        with mock.patch("tmux.run") as run:
            tmux.confirm_then('Mandar "#1" (y/n)', argv)
        run.assert_called_once_with(
            "confirm-before",
            "-p",
            'Mandar "##1" (y/n)',
            f"run-shell -b {tmux.quote_cmd(argv)}",
            timeout=20,
        )

    def test_command_part_is_escaped_like_quote_cmd(self):
        with mock.patch("tmux.run") as run:
            tmux.confirm_then("ok?", ["/bin/ws", "send", 'a"b$c#d'])
        command = run.call_args.args[3]
        self.assertEqual(command, 'run-shell -b "/bin/ws send \'a\\"b\\$c##d\'"')


class PopupTest(unittest.TestCase):
    def test_default_size_is_92_percent(self):
        with mock.patch("tmux.run") as run:
            tmux.popup("nvim x", "/repo")
        run.assert_called_once_with("display-popup", "-E", "-w", "92%", "-h", "92%", "-d",
                                    "/repo", "nvim x", timeout=None)

    def test_fullscreen_size_is_100_percent(self):
        with mock.patch("tmux.run") as run:
            tmux.popup("micro x", "/repo", size="100%")
        run.assert_called_once_with("display-popup", "-E", "-w", "100%", "-h", "100%", "-d",
                                    "/repo", "micro x", timeout=None)

    def test_client_is_targeted_when_given(self):
        with mock.patch("tmux.run") as run:
            tmux.popup("nvim x", "/repo", client="/dev/pts/3")
        run.assert_called_once_with("display-popup", "-E", "-c", "/dev/pts/3", "-w", "92%",
                                    "-h", "92%", "-d", "/repo", "nvim x", timeout=None)


class MenuTest(unittest.TestCase):
    def test_builds_one_display_menu_call(self):
        items = [("micro", "1", "display-popup -E -w 100% -h 100% -d /r 'micro x'"),
                 ("nvim", "2", "display-popup -E -w 92% -h 92% -d /r 'nvim x'")]
        with mock.patch("tmux.run") as run:
            tmux.menu("Open x with:", items)
        run.assert_called_once_with("display-menu", "-T", "Open x with:",
                                    "micro", "1", items[0][2],
                                    "nvim", "2", items[1][2], timeout=None)

    def test_hashes_in_title_and_label_are_escaped(self):
        with mock.patch("tmux.run") as run:
            tmux.menu("a#b", [("c#d", "1", "cmd")])
        self.assertEqual(run.call_args.args[:5], ("display-menu", "-T", "a##b", "c##d", "1"))
        self.assertEqual(run.call_args.args[5], "cmd")


class PrivilegedTest(unittest.TestCase):
    def outside_tmux(self, answer="s"):
        """Runs privileged() with no tmux at all; returns (code, stdout, run mock, input mock)."""
        out = io.StringIO()
        with mock.patch.dict(os.environ, {}, clear=True), \
                mock.patch("tmux.run") as run, \
                mock.patch("builtins.input", return_value=answer) as ask, \
                mock.patch("sys.stdout", out):
            code = tmux.privileged(["pkexec", "pacman", "-S", "--needed", "tmux"])
        return code, out.getvalue(), run, ask

    def test_outside_tmux_prints_the_full_command_and_runs_nothing(self):
        code, out, run, ask = self.outside_tmux()
        self.assertIsNone(code)
        run.assert_not_called()
        ask.assert_called_once_with(i18n.t("privileged.confirm"))
        self.assertIn("pkexec pacman -S --needed tmux", out)
        self.assertIn(i18n.t("privileged.not_run"), out)

    def test_outside_tmux_declined_runs_nothing(self):
        code, out, run, _ask = self.outside_tmux("n")
        self.assertIsNone(code)
        run.assert_not_called()
        self.assertIn(i18n.t("privileged.declined"), out)

    def test_outside_tmux_without_an_answer_does_not_block(self):
        out = io.StringIO()
        with mock.patch.dict(os.environ, {}, clear=True), \
                mock.patch("tmux.run") as run, \
                mock.patch("builtins.input", side_effect=EOFError), \
                mock.patch("sys.stdout", out):
            code = tmux.privileged(["pkexec", "true"])
        self.assertIsNone(code)
        run.assert_not_called()
        self.assertIn(i18n.t("privileged.declined"), out.getvalue())

    def test_clients_needs_tmux_and_an_attached_client(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(tmux.clients(), [])
        with mock.patch.dict(os.environ, {"TMUX": "/tmp/sock,1,0"}), \
                mock.patch("tmux.run", return_value=""):
            self.assertEqual(tmux.clients(), [])
        with mock.patch.dict(os.environ, {"TMUX": "/tmp/sock,1,0"}), \
                mock.patch("tmux.run", return_value="/dev/pts/3\n/dev/pts/4\n"):
            self.assertEqual(tmux.clients(), ["/dev/pts/3", "/dev/pts/4"])

    def test_popup_shows_the_command_and_returns_its_exit_code(self):
        seen = {}

        def fake_popup(cmd, cwd, size="92%", client=None):
            # tmux strips the outer quotes that quote_cmd adds for its command parser
            line = cmd[1:-1] if cmd.startswith('"') and cmd.endswith('"') else cmd
            argv = shlex.split(line)
            seen["script"] = Path(argv[1]).read_text(encoding="utf-8")
            seen["cwd"], seen["client"] = cwd, client
            subprocess.run(argv, input="\n", text=True, cwd=cwd, capture_output=True, check=False)

        with mock.patch.dict(os.environ, {"TMUX": "/tmp/sock,1,0"}), \
                mock.patch("tmux.run", return_value="/dev/pts/3"), \
                mock.patch("tmux.popup", side_effect=fake_popup):
            code = tmux.privileged(["sh", "-c", "exit 7"], cwd="/tmp")
        self.assertEqual(code, 7)
        self.assertEqual(seen["cwd"], "/tmp")
        self.assertEqual(seen["client"], "/dev/pts/3")
        self.assertIn("sh -c 'exit 7'", seen["script"])
        self.assertIn(i18n.t("privileged.title"), seen["script"])
        self.assertIn(i18n.t("privileged.exit"), seen["script"])
        self.assertIn(i18n.t("privileged.close"), seen["script"])

    def test_popup_that_never_runs_returns_none(self):
        with mock.patch.dict(os.environ, {"TMUX": "/tmp/sock,1,0"}), \
                mock.patch("tmux.run", return_value="/dev/pts/3"), \
                mock.patch("tmux.popup"):
            self.assertIsNone(tmux.privileged(["true"]))

    def test_empty_argv_does_nothing(self):
        with mock.patch("tmux.run") as run:
            self.assertIsNone(tmux.privileged([]))
        run.assert_not_called()

    def test_every_privileged_i18n_key_exists(self):
        strings = json.loads((Path(tmux.__file__).resolve().parent.parent / "i18n" / "pt-BR.json")
                             .read_text(encoding="utf-8"))
        for key in ("privileged.title", "privileged.exit", "privileged.close",
                    "privileged.outside", "privileged.confirm", "privileged.answer.yes",
                    "privileged.declined", "privileged.not_run", "privileged.usage"):
            self.assertIn(key, strings)


if __name__ == "__main__":
    unittest.main()
