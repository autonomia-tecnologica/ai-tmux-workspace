"""`installer.overlay_harness` removes only what the overlay itself installed and the repo no longer has (item 149).

Every test runs under a temporary HOME and a temporary WS_STATE: the record lives in that state dir, the copy
lives in a temporary installed-harness folder. Nothing touches ~/.local/share/ai-tmux-workspace or the real state.
"""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import installer  # noqa: E402


def tree(folder):
    """relative path -> bytes of every file under `folder` (empty when it does not exist)."""
    folder = Path(folder)
    if not folder.is_dir():
        return {}
    return {path.relative_to(folder).as_posix(): path.read_bytes()
            for path in sorted(folder.rglob("*")) if path.is_file()}


class OverlayHarnessTests(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        base = Path(self._tmp.name)
        self.home = base / "home"
        self.state = base / "state"
        self.source = base / "canonical" / "harness"
        self.target = base / "local" / "share" / "ai-tmux-workspace" / "harness"
        self.home.mkdir()
        self.source.mkdir(parents=True)
        patch = mock.patch.dict(os.environ, {"HOME": str(self.home), "WS_STATE": str(self.state)})
        patch.start()
        self.addCleanup(patch.stop)
        self.write_source({"HARNESS.md": "# Harness\n", "CLAUDE.md": "# Claude\n",
                           "forcado.yaml": "versao: 1\nitens: []\n", "ORIGEM.md": "origem\n",
                           "regras/a.md": "regra a\n", "regras/b.md": "regra b\n"})

    def write_source(self, files):
        """Replaces the canonical source with exactly `files` (relative path -> text)."""
        for path in sorted(self.source.rglob("*"), reverse=True):
            if path.is_file():
                path.unlink()
        for name, text in files.items():
            path = self.source / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")

    def record(self):
        return json.loads((self.state / "harness-overlay.json").read_text(encoding="utf-8"))["files"]

    def overlay(self):
        return installer.overlay_harness(self.source, self.target)

    def test_record_lists_exactly_what_the_overlay_installed(self):
        self.overlay()
        self.assertEqual(self.record(), ["CLAUDE.md", "HARNESS.md", "ORIGEM.md", "forcado.yaml",
                                         "regras/a.md", "regras/b.md"])
        self.assertTrue((self.state / "harness-overlay.json").is_file())
        self.assertFalse((self.state / "harness-overlay.json.tmp").exists())

    def test_file_deleted_in_the_repo_is_removed_from_the_copy_on_the_next_apply(self):
        self.overlay()
        self.assertTrue((self.target / "regras" / "b.md").is_file())
        files = tree(self.source)
        del files["regras/b.md"]
        self.write_source({name: text.decode("utf-8") for name, text in files.items()})

        self.overlay()

        self.assertFalse((self.target / "regras" / "b.md").exists())
        self.assertTrue((self.target / "regras" / "a.md").is_file())
        self.assertNotIn("regras/b.md", self.record())

    def test_stranger_file_and_declaration_file_stay(self):
        self.overlay()
        (self.target / "estranho.md").write_text("de outra fonte\n", encoding="utf-8")
        (self.target / "regras" / "meu.md").write_text("minha regra\n", encoding="utf-8")
        # forcado.yaml was installed by the overlay and then left the repo: it is still never removed.
        self.write_source({name: text for name, text in {
            "HARNESS.md": "# Harness\n", "CLAUDE.md": "# Claude\n", "ORIGEM.md": "origem\n",
            "regras/a.md": "regra a\n"}.items()})

        self.overlay()

        self.assertEqual((self.target / "estranho.md").read_text(encoding="utf-8"), "de outra fonte\n")
        self.assertEqual((self.target / "regras" / "meu.md").read_text(encoding="utf-8"), "minha regra\n")
        self.assertEqual((self.target / "forcado.yaml").read_text(encoding="utf-8"), "versao: 1\nitens: []\n")
        self.assertTrue((self.target / "source.json").is_file())
        # The last apply did not install forcado.yaml, so the record no longer lists it.
        self.assertNotIn("forcado.yaml", self.record())

    def test_second_apply_changes_nothing(self):
        self.overlay()
        first_copy = tree(self.target)
        first_record = (self.state / "harness-overlay.json").read_bytes()

        self.overlay()
        self.overlay()

        self.assertEqual(tree(self.target), first_copy)
        self.assertEqual((self.state / "harness-overlay.json").read_bytes(), first_record)

    def test_failure_in_the_middle_keeps_the_previous_record(self):
        self.overlay()
        previous = self.record()
        files = tree(self.source)
        del files["regras/b.md"]
        files["regras/c.md"] = b"regra c\n"
        self.write_source({name: text.decode("utf-8") for name, text in files.items()})
        real_write_over = installer._write_over

        def failing(source, destination):
            if destination.name == "c.md":
                raise OSError("disco cheio")
            return real_write_over(source, destination)

        with mock.patch.object(installer, "_write_over", side_effect=failing):
            with self.assertRaises(OSError):
                self.overlay()

        after_failure = self.record()
        self.assertTrue(set(previous) <= set(after_failure))
        self.assertTrue((self.target / "regras" / "b.md").is_file())

        self.overlay()

        self.assertEqual(self.record(), ["CLAUDE.md", "HARNESS.md", "ORIGEM.md", "forcado.yaml",
                                         "regras/a.md", "regras/c.md"])
        self.assertFalse((self.target / "regras" / "b.md").exists())
        self.assertEqual((self.target / "regras" / "c.md").read_text(encoding="utf-8"), "regra c\n")

    def test_unreadable_record_stops_before_touching_the_copy(self):
        self.overlay()
        (self.state / "harness-overlay.json").write_text("{nao e json", encoding="utf-8")
        files = tree(self.source)
        del files["regras/b.md"]
        self.write_source({name: text.decode("utf-8") for name, text in files.items()})
        before = tree(self.target)

        with self.assertRaises(OSError):
            self.overlay()

        self.assertEqual(tree(self.target), before)
        self.assertEqual((self.state / "harness-overlay.json").read_text(encoding="utf-8"), "{nao e json")

    def test_missing_source_is_an_error_and_removes_nothing(self):
        self.overlay()
        before = tree(self.target)
        with self.assertRaises(OSError):
            installer.overlay_harness(self.source.parent / "inexistente", self.target)
        self.assertEqual(tree(self.target), before)


if __name__ == "__main__":
    unittest.main()
