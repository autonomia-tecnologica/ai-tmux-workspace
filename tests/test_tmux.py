"""Tests for the tmux command-string helpers. tmux.run is mocked, so no tmux server is touched."""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

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
            tmux.popup("dawn x", "/repo", size="100%")
        run.assert_called_once_with("display-popup", "-E", "-w", "100%", "-h", "100%", "-d",
                                    "/repo", "dawn x", timeout=None)


class MenuTest(unittest.TestCase):
    def test_builds_one_display_menu_call(self):
        items = [("Dawn", "1", "display-popup -E -w 100% -h 100% -d /r 'dawn x'"),
                 ("nvim", "2", "display-popup -E -w 92% -h 92% -d /r 'nvim x'")]
        with mock.patch("tmux.run") as run:
            tmux.menu("Abrir x com:", items)
        run.assert_called_once_with("display-menu", "-T", "Abrir x com:",
                                    "Dawn", "1", items[0][2],
                                    "nvim", "2", items[1][2], timeout=None)

    def test_hashes_in_title_and_label_are_escaped(self):
        with mock.patch("tmux.run") as run:
            tmux.menu("a#b", [("c#d", "1", "cmd")])
        self.assertEqual(run.call_args.args[:5], ("display-menu", "-T", "a##b", "c##d", "1"))
        self.assertEqual(run.call_args.args[5], "cmd")


if __name__ == "__main__":
    unittest.main()
