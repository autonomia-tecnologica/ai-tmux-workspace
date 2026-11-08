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


if __name__ == "__main__":
    unittest.main()
