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


def make(text, **kwargs):
    return viewer.Viewer(lambda v: ([], []), footer=lambda v: text, **kwargs)


class FooterTest(unittest.TestCase):
    def test_a_long_message_wraps_into_more_than_the_old_two_rows(self):
        lines = make("m" * 65)._footer_lines(20, viewer.MAX_FOOTER_LINES)
        self.assertGreater(len(lines), 2)
        self.assertTrue(all(viewer._width(l) <= 19 for l in lines))

    def test_the_footer_keeps_the_end_when_it_does_not_fit(self):
        text = "".join(f"row {n} " for n in range(30))
        lines = make(text)._footer_lines(20, 4)
        self.assertEqual(len(lines), 4)
        self.assertTrue(lines[0].startswith("…"))
        self.assertIn("29", "".join(lines))      # the end survives, the start does not

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
            v._draw(force=True)
        self.assertGreater(v._footer_rows, 3)
        self.assertEqual(v._footer_h(), v._footer_rows)

    def test_a_long_path_keeps_every_line_when_the_cap_is_off(self):
        text = "/" + "/".join(["directory"] * 20)
        lines = make(text, max_footer_lines=None)._footer_lines(20, 100)
        self.assertGreater(len(lines), viewer.MAX_FOOTER_LINES)
        self.assertEqual("".join(lines), text)

    def test_the_drawn_frame_uses_the_whole_height_when_the_cap_is_off(self):
        text = f"{viewer.B}" + "/".join(["d" * 20] * 20) + viewer.R
        v = make(text, max_footer_lines=None)
        out = io.StringIO()
        with mock.patch("shutil.get_terminal_size",
                        return_value=os.terminal_size((20, 30))), \
                mock.patch("sys.stdout", out):
            v._draw(force=True)
        self.assertGreater(v._footer_rows, viewer.MAX_FOOTER_LINES + 1)

    def test_the_default_cap_still_limits_the_drawn_footer(self):
        text = "m" * 1000
        v = make(text)
        out = io.StringIO()
        with mock.patch("shutil.get_terminal_size",
                        return_value=os.terminal_size((20, 30))), \
                mock.patch("sys.stdout", out):
            v._draw(force=True)
        self.assertEqual(v._footer_rows, viewer.MAX_FOOTER_LINES + 1)


if __name__ == "__main__":
    unittest.main()
