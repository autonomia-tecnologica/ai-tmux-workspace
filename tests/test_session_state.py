"""Tests for the persistent session tabs per repo (`src/session_state.py`).

Every test points WS_STATE at a temporary folder, so the real cache is never read or written. The
module and its CLI are exercised directly; `bin/workspace` calls the same CLI."""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
sys.path.insert(0, str(SRC))

import session_state  # noqa: E402


class SessionStateTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name).resolve()
        self.state = self.root / "state"
        self.repo = self.root / "repo"
        self.state.mkdir()
        self.repo.mkdir()
        env = mock.patch.dict(os.environ, {"WS_STATE": str(self.state)})
        env.start()
        self.addCleanup(env.stop)

    def run_cli(self, *args, stdin=""):
        proc = subprocess.run([sys.executable, str(SRC / "session_state.py"), *args],
                              input=stdin, capture_output=True, text=True,
                              env=dict(os.environ, WS_STATE=str(self.state)), check=False)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return proc.stdout

    # --- storage ----------------------------------------------------------------------------
    def test_missing_and_corrupt_files_read_as_empty(self):
        self.assertEqual(session_state.read(self.repo),
                         {"repo": str(self.repo), "active": "", "tabs": []})
        path = session_state.state_path(self.repo)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{not json", encoding="utf-8")
        self.assertEqual(session_state.read(self.repo)["tabs"], [])

    def test_state_lives_under_ws_state_tabs_keyed_by_real_path(self):
        session_state.add(str(self.repo), "tab-1", "claude")
        link = self.root / "link"
        link.symlink_to(self.repo)
        self.assertEqual(session_state.state_path(link), session_state.state_path(self.repo))
        self.assertEqual(session_state.state_path(self.repo).parent, self.state / "tabs")
        data = json.loads(session_state.state_path(self.repo).read_text(encoding="utf-8"))
        self.assertEqual(data["repo"], str(self.repo))

    def test_add_keeps_the_order_and_sets_the_active_tab(self):
        session_state.add(str(self.repo), "tab-1", "claude")
        session_state.add(str(self.repo), "tab-2", "opencode-free")

        data = session_state.read(self.repo)
        self.assertEqual([tab["id"] for tab in data["tabs"]], ["tab-1", "tab-2"])
        self.assertEqual([tab["profile"] for tab in data["tabs"]], ["claude", "opencode-free"])
        self.assertEqual(data["active"], "tab-2")

        session_state.add(str(self.repo), "tab-1", "claude")
        self.assertEqual([tab["id"] for tab in session_state.read(self.repo)["tabs"]],
                         ["tab-1", "tab-2"])
        self.assertEqual(session_state.read(self.repo)["active"], "tab-1")

    def test_add_without_a_session_keeps_the_recorded_one(self):
        session_state.add(str(self.repo), "tab-1", "claude", "sess-1")
        session_state.add(str(self.repo), "tab-1", "claude")
        self.assertEqual(session_state.session_for(self.repo, "tab-1"), "sess-1")

    def test_remove_updates_the_active_tab_and_drops_its_live_session(self):
        session_state.add(str(self.repo), "tab-1", "claude")
        session_state.add(str(self.repo), "tab-2", "claude")
        live = self.state / "tabs" / "session-tab-2"
        live.write_text("sess-2", encoding="utf-8")
        session_state.remove(str(self.repo), "tab-2")
        self.assertFalse(live.exists())
        data = session_state.read(self.repo)
        self.assertEqual(data["tabs"], [{"id": "tab-1", "profile": "claude", "session": ""}])
        self.assertEqual(data["active"], "tab-1")
        session_state.remove(str(self.repo), "tab-1")
        self.assertEqual(session_state.read(self.repo)["active"], "")

    def test_only_tabs_with_an_id_are_kept(self):
        path = session_state.state_path(self.repo)
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({"active": "", "tabs": [
            {"id": "", "profile": "x"}, {"id": "a", "profile": "claude"},
            "bad", {"id": "a", "profile": "other"}]}), encoding="utf-8")
        self.assertEqual(session_state.read(self.repo)["tabs"],
                         [{"id": "a", "profile": "claude", "session": ""}])

    # --- session id -------------------------------------------------------------------------
    def test_live_session_file_wins_over_the_recorded_value(self):
        session_state.add(str(self.repo), "tab-1", "claude", "stored")
        (self.state / "tabs" / "session-tab-1").write_text("live\n", encoding="utf-8")
        self.assertEqual(session_state.session_for(self.repo, "tab-1"), "live")

    def test_reset_session_clears_the_file_and_records_the_new_id(self):
        session_state.add(str(self.repo), "tab-1", "claude", "old")
        (self.state / "tabs" / "session-tab-1").write_text("old-live", encoding="utf-8")
        session_state.reset_session(str(self.repo), "tab-1", "new")
        self.assertFalse((self.state / "tabs" / "session-tab-1").exists())
        self.assertEqual(session_state.session_for(self.repo, "tab-1"), "new")
        session_state.reset_session(str(self.repo), "tab-1")
        self.assertEqual(session_state.session_for(self.repo, "tab-1"), "")

    def test_set_session_only_changes_an_existing_tab(self):
        session_state.set_session(str(self.repo), "missing", "sess-1")
        self.assertEqual(session_state.read(self.repo)["tabs"], [])
        session_state.add(str(self.repo), "tab-1", "claude")
        session_state.set_session(str(self.repo), "tab-1", "sess-1")
        self.assertEqual(session_state.session_for(self.repo, "tab-1"), "sess-1")

    def test_save_replaces_the_list_and_preserves_known_sessions(self):
        session_state.add(str(self.repo), "tab-1", "claude", "sess-1")
        session_state.add(str(self.repo), "tab-2", "opencode", "sess-2")
        (self.state / "tabs" / "session-tab-2").write_text("live-2", encoding="utf-8")
        session_state.save(str(self.repo), [("tab-2", "opencode"), ("tab-3", "claude")],
                           active="tab-3")
        data = session_state.read(self.repo)
        self.assertEqual([(tab["id"], tab["profile"]) for tab in data["tabs"]],
                         [("tab-2", "opencode"), ("tab-3", "claude")])
        self.assertEqual(data["active"], "tab-3")
        self.assertEqual(session_state.session_for(self.repo, "tab-2"), "live-2")
        self.assertEqual(session_state.session_for(self.repo, "tab-3"), "")

    # --- CLI --------------------------------------------------------------------------------
    def test_cli_lists_active_and_tabs_in_order(self):
        session_state.add(str(self.repo), "tab-1", "claude", "sess-1")
        session_state.add(str(self.repo), "tab-2", "opencode-free")
        out = self.run_cli("list", str(self.repo))
        self.assertEqual(out.splitlines(), [
            "active\ttab-2",
            "id\ttab-1\tclaude\tsess-1",
            "id\ttab-2\topencode-free\t",
        ])

    def test_cli_save_reads_id_and_profile_pairs_from_stdin(self):
        self.run_cli("save", str(self.repo), "tab-2", stdin="tab-1\tclaude\ntab-2\topencode\n")
        data = session_state.read(self.repo)
        self.assertEqual([tab["id"] for tab in data["tabs"]], ["tab-1", "tab-2"])
        self.assertEqual(data["active"], "tab-2")

    def test_cli_prints_a_new_id(self):
        tab_id = self.run_cli("new-id").strip()
        self.assertTrue(tab_id)
        self.assertNotEqual(tab_id, self.run_cli("new-id").strip())


if __name__ == "__main__":
    unittest.main()
