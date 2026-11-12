"""Darwin coverage for the platform adapters: the OS opener, notifications, the clipboard and the
Linux-only dictation gate.

Nothing here needs a Mac or runs a real `open`/`osascript`: `sys.platform` (and `platform.system`)
is patched and the built argv is inspected. The same cases assert the macOS branches never fall back
to the Linux tools (`xdg-open`, `notify-send`, `xclip`, `wl-copy`).
"""
import os
import sys
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
sys.path.insert(0, str(SRC))

import clipboard  # noqa: E402
import i18n  # noqa: E402
import mic  # noqa: E402
import notify  # noqa: E402
import opener  # noqa: E402
import options  # noqa: E402
import stt  # noqa: E402

LINUX_TOOLS = ("xdg-open", "notify-send", "xclip", "wl-copy", "wslview", "explorer.exe")


@contextmanager
def patched_platform(name, system):
    """Patch both platform probes for the block (the adapters read `sys.platform`)."""
    with mock.patch.object(sys, "platform", name), mock.patch("platform.system", return_value=system):
        yield


def which_for(*commands):
    """shutil.which stand-in that only finds `commands` (as /usr/bin/<command>)."""
    def which(name):
        return f"/usr/bin/{name}" if name in commands else None
    return which


def which_must_not_be_called(name):
    raise AssertionError(f"which({name!r}) was called on the macOS branch")


class OpenerDarwinTest(unittest.TestCase):
    def test_darwin_uses_open_and_keeps_the_path_as_one_argument(self):
        path = "/tmp/Relatório final.pdf"
        self.assertEqual(opener.build_command(path, platform="darwin"), ["open", path])

    def test_darwin_never_asks_for_the_linux_opener(self):
        cmd = opener.build_command("/tmp/a b.pdf", platform="darwin", which=which_must_not_be_called)
        self.assertEqual(cmd, ["open", "/tmp/a b.pdf"])
        for tool in LINUX_TOOLS:
            self.assertNotIn(tool, cmd)

    def test_the_default_platform_comes_from_sys_platform(self):
        with patched_platform("darwin", "Darwin"):
            self.assertEqual(opener.build_command("/tmp/a.pdf"), ["open", "/tmp/a.pdf"])

    def test_linux_still_uses_xdg_open(self):
        cmd = opener.build_command("/tmp/a.pdf", platform="linux", wsl=False,
                                   which=which_for("xdg-open"))
        self.assertEqual(cmd, ["xdg-open", "/tmp/a.pdf"])

    def test_open_system_spawns_open_on_darwin(self):
        with patched_platform("darwin", "Darwin"), \
                mock.patch.object(opener, "_spawn", return_value=True) as spawn:
            self.assertTrue(opener.open_system("/tmp/a.pdf"))
        spawn.assert_called_once_with(["open", "/tmp/a.pdf"])


class NotifyDarwinTest(unittest.TestCase):
    def test_darwin_builds_osascript_with_escaped_strings(self):
        cmd = notify.build_command('Título "x"', "linha 1\nlinha 2", platform="darwin")
        self.assertEqual(cmd[:2], ["osascript", "-e"])
        self.assertEqual(len(cmd), 3)
        script = cmd[2]
        self.assertIn('with title "Título \\"x\\""', script)
        self.assertIn("linha 1\\nlinha 2", script)
        self.assertNotIn("\n", script)          # a raw newline would break the literal
        self.assertNotIn("notify-send", script)

    def test_darwin_escapes_backslash_carriage_return_and_tab(self):
        script = notify.build_command("a\\b", "c\td\re", platform="darwin")[2]
        self.assertIn("a\\\\b", script)
        self.assertIn("c\\td\\re", script)

    def test_null_bytes_become_spaces(self):
        script = notify.build_command("a\0b", "c\0d", platform="darwin")[2]
        self.assertNotIn("\0", script)
        self.assertIn("a b", script)
        self.assertIn("c d", script)

    def test_the_default_platform_comes_from_sys_platform(self):
        with patched_platform("darwin", "Darwin"):
            cmd = notify.build_command("t", "b")
        self.assertEqual(cmd[0], "osascript")

    def test_linux_still_uses_notify_send(self):
        cmd = notify.build_command("t", "b", platform="linux", wsl=False)
        self.assertEqual(cmd[0], "notify-send")
        self.assertNotIn("osascript", cmd)

    def test_notify_spawns_osascript_detached(self):
        with patched_platform("darwin", "Darwin"), \
                mock.patch.object(notify.subprocess, "Popen") as popen:
            notify.notify("t", "b")
        cmd = popen.call_args[0][0]
        self.assertEqual(cmd[0], "osascript")
        self.assertTrue(popen.call_args[1]["start_new_session"])
        for tool in LINUX_TOOLS:
            self.assertNotIn(tool, cmd)


class ClipboardDarwinTest(unittest.TestCase):
    def test_darwin_quotes_the_absolute_posix_path(self):
        path = '/tmp/It\'s a "final".pdf'
        cmd, data = clipboard.build_command(path, platform="darwin")
        self.assertEqual(cmd[:2], ["osascript", "-e"])
        self.assertIsNone(data)
        self.assertEqual(cmd[2],
                         'set the clipboard to (POSIX file "/tmp/It\'s a \\"final\\".pdf")')

    def test_relative_paths_are_made_absolute(self):
        expected = os.path.join(os.getcwd(), "relatorio.pdf")
        cmd, _ = clipboard.build_command("relatorio.pdf", platform="darwin")
        self.assertIn(expected, cmd[2])

    def test_darwin_never_asks_for_the_linux_tools(self):
        cmd, _ = clipboard.build_command("/tmp/a b.pdf", platform="darwin",
                                         which=which_must_not_be_called)
        self.assertEqual(cmd[0], "osascript")
        for tool in LINUX_TOOLS:
            self.assertNotIn(tool, cmd)

    def test_the_default_platform_comes_from_sys_platform(self):
        with patched_platform("darwin", "Darwin"):
            cmd, _ = clipboard.build_command("/tmp/a.pdf")
        self.assertEqual(cmd[0], "osascript")

    def test_linux_still_uses_xclip_or_wl_copy(self):
        cmd, data = clipboard.build_command("/tmp/a b.pdf", platform="linux",
                                            which=which_for("xclip"))
        self.assertEqual(cmd, ["xclip", "-selection", "clipboard", "-t", "text/uri-list"])
        self.assertEqual(data, b"file:///tmp/a%20b.pdf\r\n")
        wl, _ = clipboard.build_command("/tmp/a.pdf", platform="linux", which=which_for("wl-copy"))
        self.assertEqual(wl[0], "wl-copy")

    def test_copy_file_runs_osascript_only(self):
        with patched_platform("darwin", "Darwin"), \
                mock.patch.object(clipboard, "_run", return_value=True) as run:
            self.assertEqual(clipboard.copy_file("/tmp/a.pdf"), "file")
        cmd, data = run.call_args[0]
        self.assertEqual(cmd[0], "osascript")
        self.assertIsNone(data)
        for tool in LINUX_TOOLS:
            self.assertNotIn(tool, cmd)


class DictationGateTest(unittest.TestCase):
    def test_available_only_on_linux(self):
        self.assertTrue(mic.available("linux"))
        self.assertTrue(mic.available("linux2"))
        self.assertFalse(mic.available("darwin"))
        self.assertFalse(mic.available("win32"))

    def test_available_reads_sys_platform(self):
        with patched_platform("darwin", "Darwin"):
            self.assertFalse(mic.available())
        with patched_platform("linux", "Linux"):
            self.assertTrue(mic.available())

    def test_start_stt_starts_nothing_off_linux(self):
        with patched_platform("darwin", "Darwin"), \
                mock.patch.object(mic, "docker_ready") as docker, \
                mock.patch.object(mic.subprocess, "Popen") as popen:
            self.assertEqual(mic.start_stt(), (None, None, i18n.t("mic.no_arecord")))
        docker.assert_not_called()
        popen.assert_not_called()

    def test_run_prints_the_unavailable_message_without_recording(self):
        with patched_platform("darwin", "Darwin"), \
                mock.patch("builtins.print") as printed, \
                mock.patch.object(mic.time, "sleep") as sleep, \
                mock.patch.object(mic.subprocess, "Popen") as popen:
            mic.run("%1")
        printed.assert_called_once_with(i18n.t("mic.no_arecord"))
        sleep.assert_called_once_with(2)
        popen.assert_not_called()

    def test_linux_model_paths_are_unchanged(self):
        self.assertEqual(mic.default_ggml_model("linux"),
                         "/mnt/Data/LLM/at-whisper/ggml/ggml-small.bin")
        self.assertEqual(stt.default_models("linux"), "/mnt/Data/LLM/at-whisper/models")

    def test_other_platform_model_paths_leave_the_linux_install(self):
        for name in ("darwin", "win32"):
            with self.subTest(platform=name):
                self.assertNotIn("/mnt/Data", mic.default_ggml_model(name))
                self.assertNotIn("/mnt/Data", stt.default_models(name))
                self.assertTrue(mic.default_ggml_model(name).startswith(os.path.expanduser("~")))

    def test_stt_returns_not_available_before_loading_the_model(self):
        with patched_platform("darwin", "Darwin"), mock.patch("builtins.print") as printed:
            self.assertEqual(stt.main(), 1)
        printed.assert_called_once_with(i18n.t("mic.no_arecord"), file=sys.stderr)


class OptionsDarwinTest(unittest.TestCase):
    def setUp(self):
        patches = [
            mock.patch.object(options.lib, "read_config", return_value={}),
            mock.patch.object(options.lib, "CONFIG_ERROR", None),
            mock.patch.object(options.lib, "needs_migration", return_value=False),
            mock.patch.object(options.lib, "profile_names", return_value=["claude"]),
            mock.patch.object(options.lib, "agent_name", return_value="claude"),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)

    def test_darwin_hides_the_dictation_folder_and_leaf(self):
        with patched_platform("darwin", "Darwin"):
            nodes = options.tree()
        labels = [node.get("label") for node in nodes]
        self.assertNotIn(i18n.t("options.general"), labels)
        self.assertNotIn(i18n.t("options.mic"), labels)
        self.assertEqual([node for _key, _depth, node in options.flatten(nodes) if "mic" in node], [])

    def test_linux_keeps_the_dictation_leaf(self):
        with patched_platform("linux", "Linux"):
            nodes = options.tree()
        general = next(node for node in nodes if node.get("label") == i18n.t("options.general"))
        self.assertEqual([child["label"] for child in general["children"]], [i18n.t("options.mic")])
        self.assertTrue(general["children"][0]["mic"])


class ConfigExampleTest(unittest.TestCase):
    def test_free_folder_examples_are_macos_names(self):
        text = (ROOT / "config" / "config.example.yaml").read_text(encoding="utf-8")
        block = text.split("# free_folders:", 1)[1].split("\n\n", 1)[0]
        self.assertIn("#   - ~/Documents", block)
        self.assertIn("#   - path: ~/Pictures", block)
        self.assertIn("#   - ~/Movies", block)
        for linux_example in ("#   - ~/Documentos", "#   - path: ~/Imagens", "#   - ~/Vídeos"):
            self.assertNotIn(linux_example, block)


if __name__ == "__main__":
    unittest.main()
