"""clipboard.copy_text: the command that puts plain text on the OS clipboard.

Nothing here touches a real clipboard: the platform and the tools are patched, and the built argv
and stdin are inspected.
"""
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import clipboard  # noqa: E402


def which_for(*commands):
    def which(name):
        return f"/usr/bin/{name}" if name in commands else None
    return which


class CopyTextCommandTest(unittest.TestCase):
    def test_darwin_uses_pbcopy_with_utf8_stdin(self):
        cmd, data = clipboard.build_text_command("/tmp/Relatório final.pdf", platform="darwin")
        self.assertEqual(cmd, ["pbcopy"])
        self.assertEqual(data, "/tmp/Relatório final.pdf".encode("utf-8"))

    def test_linux_wayland_uses_wl_copy_as_text_plain(self):
        cmd, data = clipboard.build_text_command("/tmp/a b.pdf", platform="linux",
                                                 which=which_for("wl-copy", "xclip"))
        self.assertEqual(cmd, ["wl-copy", "--type", "text/plain"])
        self.assertEqual(data, b"/tmp/a b.pdf")

    def test_linux_x11_uses_xclip_clipboard_as_text_plain(self):
        cmd, data = clipboard.build_text_command("/tmp/a.pdf", platform="linux",
                                                 which=which_for("xclip"))
        self.assertEqual(cmd, ["xclip", "-selection", "clipboard", "-t", "text/plain"])
        self.assertEqual(data, b"/tmp/a.pdf")

    def test_no_tool_returns_none(self):
        self.assertIsNone(clipboard.build_text_command("/tmp/a.pdf", platform="linux",
                                                       which=which_for()))

    def test_unknown_platform_returns_none(self):
        self.assertIsNone(clipboard.build_text_command("/tmp/a.pdf", platform="win32",
                                                       which=which_for("xclip")))

    def test_copy_text_runs_the_built_command(self):
        with mock.patch.object(clipboard, "build_text_command",
                               return_value=(["pbcopy"], b"/tmp/a.pdf")) as build, \
                mock.patch.object(clipboard, "_run", return_value=True) as run:
            self.assertEqual(clipboard.copy_text("/tmp/a.pdf"), "text")
        build.assert_called_once_with("/tmp/a.pdf")
        run.assert_called_once_with(["pbcopy"], b"/tmp/a.pdf")

    def test_copy_text_without_tool_returns_none_and_runs_nothing(self):
        with mock.patch.object(clipboard, "build_text_command", return_value=None), \
                mock.patch.object(clipboard, "_run") as run:
            self.assertIsNone(clipboard.copy_text("/tmp/a.pdf"))
        run.assert_not_called()

    def test_copy_text_returns_none_when_the_tool_fails(self):
        with mock.patch.object(clipboard, "build_text_command",
                               return_value=(["xclip"], b"x")), \
                mock.patch.object(clipboard, "_run", return_value=False):
            self.assertIsNone(clipboard.copy_text("/tmp/a.pdf"))


if __name__ == "__main__":
    unittest.main()
