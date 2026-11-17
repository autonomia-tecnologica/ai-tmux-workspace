"""Tests for the wrap of the repos list: up on the first selectable row goes to the last, down on the last
goes to the first, with j/k and the arrows. The Viewer owns the rule; the repos panel only turns it on."""
import os
import runpy
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

SRC = Path(__file__).resolve().parent.parent / "src"
sys.path.insert(0, str(SRC))

import viewer  # noqa: E402
import lib  # noqa: E402
import yaml_min  # noqa: E402

UP = (b"k", b"\x1b[A", b"\x1bOA")
DOWN = (b"j", b"\x1b[B", b"\x1bOB")


def keyed_viewer(wrap):
    """A viewer over a list shaped like the repos panel: a group row, then repo rows, all selectable."""
    body = [viewer.Row(" group", ("g", "Work"))] + [viewer.Row(f" repo{i}", ("r", f"/x/repo{i}"))
                                                     for i in range(3)]
    v = viewer.Viewer(lambda _v: ([], body), cursor=True, wrap=wrap)
    v._size = (80, 24)
    v.header, v.body = [], body
    v.idx, v.key = 0, body[0].key
    return v


def press(v, seq):
    v._process(seq)
    return v.idx


class ViewerWrapTest(unittest.TestCase):
    def test_up_on_the_first_row_goes_to_the_last_with_every_up_key(self):
        for seq in UP:
            v = keyed_viewer(wrap=True)
            self.assertEqual(press(v, seq), 3, seq)
            self.assertEqual(v.key, ("r", "/x/repo2"), seq)

    def test_down_on_the_last_row_goes_to_the_first_with_every_down_key(self):
        for seq in DOWN:
            v = keyed_viewer(wrap=True)
            v.idx, v.key = 3, ("r", "/x/repo2")
            self.assertEqual(press(v, seq), 0, seq)
            self.assertEqual(v.key, ("g", "Work"), seq)

    def test_without_wrap_both_ends_stop(self):
        v = keyed_viewer(wrap=False)
        self.assertEqual(press(v, UP[0]), 0)
        v.idx, v.key = 3, ("r", "/x/repo2")
        self.assertEqual(press(v, DOWN[0]), 3)

    def test_the_wrap_is_symmetric_one_step_each_way(self):
        v = keyed_viewer(wrap=True)
        self.assertEqual(press(v, UP[0]), 3)     # first -> last
        self.assertEqual(press(v, DOWN[0]), 0)   # last -> first
        self.assertEqual(press(v, DOWN[0]), 1)   # then ordinary steps again
        self.assertEqual(press(v, UP[0]), 0)


class ReposPanelWiringTest(unittest.TestCase):
    """The repos panel's own Viewer (its __main__ block) is created with wrap on."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name)
        (root / "Codes" / "alpha").mkdir(parents=True)
        config = root / "config.yaml"
        config.write_text(yaml_min.dumps({"repos_folder": str(root / "Codes")}), encoding="utf-8")
        old = {name: os.environ.get(name) for name in ("AT_WS_CONFIG", "WS_STATE", "TMUX_PANE")}
        os.environ["AT_WS_CONFIG"] = str(config)
        os.environ["WS_STATE"] = str(root / "state")
        os.environ.pop("TMUX_PANE", None)

        def restore():
            for name, value in old.items():
                if value is None:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = value

        self.addCleanup(restore)

    def test_the_panel_viewer_is_created_with_wrap_on(self):
        created = []

        def capture_run(v):
            created.append(v)

        script = SRC / "panel_repos.py"
        with mock.patch.object(lib, "_fetch_bg"), \
                mock.patch.object(viewer.Viewer, "run", autospec=True, side_effect=capture_run):
            runpy.run_path(str(script), run_name="__main__")
        self.assertEqual(len(created), 1)
        self.assertTrue(created[0].wrap)


if __name__ == "__main__":
    unittest.main()
