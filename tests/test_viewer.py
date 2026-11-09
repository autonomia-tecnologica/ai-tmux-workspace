"""Tests for the viewer footer: the message box grows with the text and keeps the end visible."""
import io
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

SRC = Path(__file__).resolve().parent.parent / "src"
sys.path.insert(0, str(SRC))

import viewer  # noqa: E402


def make(text):
    return viewer.Viewer(lambda v: ([], []), rodape=lambda v: text)


class FooterTest(unittest.TestCase):
    def test_a_long_message_wraps_into_more_than_the_old_two_rows(self):
        linhas = make("m" * 65)._footer_lines(20, viewer.MAX_FOOTER_LINES)
        self.assertGreater(len(linhas), 2)
        self.assertTrue(all(viewer._largura(l) <= 19 for l in linhas))

    def test_the_footer_keeps_the_end_when_it_does_not_fit(self):
        text = "".join(f"linha {n} " for n in range(30))
        linhas = make(text)._footer_lines(20, 4)
        self.assertEqual(len(linhas), 4)
        self.assertTrue(linhas[0].startswith("…"))
        self.assertIn("linha 29", linhas[-1])

    def test_ansi_is_preserved(self):
        text = f"{viewer.ORANGE}abc{viewer.R}"
        self.assertEqual(make(text)._footer_lines(20, 10), [text])

    def test_an_empty_footer_has_no_rows(self):
        self.assertEqual(make("")._footer_lines(20, 10), [])

    def test_the_drawn_frame_gives_the_footer_more_height(self):
        v = make("m" * 65)
        out = io.StringIO()
        with mock.patch("shutil.get_terminal_size",
                        return_value=os.terminal_size((20, 12))), \
                mock.patch("sys.stdout", out):
            v._desenhar(forcar=True)
        self.assertGreater(v._rod_n, 3)
        self.assertEqual(v._rod_h(), v._rod_n)


if __name__ == "__main__":
    unittest.main()
