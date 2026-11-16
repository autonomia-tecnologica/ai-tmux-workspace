"""Tests for the shared git state of the repos panel (src/lib.py: git_state, refresh_git_states).

Every test points AT_WS_CONFIG and WS_STATE at a temporary folder, so the real state is never read or
written. Two "panels" are two calls on the same cache file: one takes the lock and reads git, the other
reads the file. git itself is replaced by a counter, except in the one test that runs real git.
"""
import fcntl
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

SRC = Path(__file__).resolve().parent.parent / "src"
sys.path.insert(0, str(SRC))

import lib  # noqa: E402
import panel_repos  # noqa: E402


class SharedGitStateTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name).resolve()
        old = {name: os.environ.get(name) for name in ("AT_WS_CONFIG", "WS_STATE")}
        os.environ["AT_WS_CONFIG"] = str(self.root / "config.yaml")
        os.environ["WS_STATE"] = str(self.root / "state")

        def restore():
            for name, value in old.items():
                if value is None:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = value

        self.addCleanup(restore)
        self.addCleanup(lib._GIT_WANTED.clear)
        lib._GIT_WANTED.clear()
        self.reads = []

    def fake_git_read(self, repo):
        self.reads.append(repo)
        return "ok", False

    def cache_file(self):
        return self.root / "state" / lib.GIT_STATE_FILE

    def age_cache(self, seconds):
        """Moves every entry of the cache `seconds` into the past (no sleeping in the tests)."""
        data = json.loads(self.cache_file().read_text(encoding="utf-8"))
        for entry in data.values():
            entry["ts"] -= seconds
        self.cache_file().write_text(json.dumps(data), encoding="utf-8")


class TwoPanelsOneReaderTest(SharedGitStateTest):
    def test_second_panel_reads_the_cache_and_git_runs_once_per_ttl(self):
        repos = ["/r/a", "/r/b"]
        with mock.patch.object(lib, "_git_read", side_effect=self.fake_git_read), \
                mock.patch.object(lib, "_kick_refresh"):
            # panel 1 finds nothing in the cache: it takes the lock and reads git for both repos
            self.assertEqual(lib.refresh_git_states(repos), 2)
            self.assertEqual(self.reads, ["/r/a", "/r/b"])
            # panel 2, same TTL: it reads the file, git does not run
            self.assertEqual(lib.git_state("/r/a"), ("ok", False))
            self.assertEqual(lib.refresh_git_states(repos), 0)
            self.assertEqual(len(self.reads), 2)
            # after the TTL, one round again, not one per panel
            self.age_cache(lib.GH_TTL + 1)
            self.assertEqual(lib.refresh_git_states(repos), 2)
            self.assertEqual(lib.refresh_git_states(repos), 0)
            self.assertEqual(len(self.reads), 4)

    def test_eight_panels_in_a_row_cost_one_round(self):
        repos = [f"/r/{i}" for i in range(39)]
        with mock.patch.object(lib, "_git_read", side_effect=self.fake_git_read), \
                mock.patch.object(lib, "_kick_refresh"):
            rounds = sum(1 for _ in range(8) if lib.refresh_git_states(repos))
        self.assertEqual(rounds, 1)
        self.assertEqual(len(self.reads), 39)

    def test_lock_held_by_another_process_means_no_git_here(self):
        lock_path = self.root / "state" / lib.GIT_STATE_LOCK
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        other = open(lock_path, "a+", encoding="utf-8")
        self.addCleanup(other.close)
        fcntl.flock(other, fcntl.LOCK_EX)   # the other process: it is reading git right now
        with mock.patch.object(lib, "_git_read", side_effect=self.fake_git_read):
            self.assertEqual(lib.refresh_git_states(["/r/a"]), 0)
        self.assertEqual(self.reads, [])
        fcntl.flock(other, fcntl.LOCK_UN)
        with mock.patch.object(lib, "_git_read", side_effect=self.fake_git_read):
            self.assertEqual(lib.refresh_git_states(["/r/a"]), 1)


class PanelNeverWaitsForGitTest(SharedGitStateTest):
    def test_first_draw_returns_pending_at_once_while_git_is_slow(self):
        release = threading.Event()

        def slow_git(repo):
            release.wait(10)
            return "ok", True

        with mock.patch.object(lib, "_git_read", side_effect=slow_git):
            started = time.time()
            state, dirty = lib.git_state("/r/slow")
            elapsed = time.time() - started
            self.assertLess(elapsed, 1.0)
            self.assertEqual((state, dirty), (lib.PENDING, False))
            self.assertEqual(panel_repos.git_icon("/r/slow"), "")
            release.set()
            lib._GIT_THREAD.join(timeout=10)
        self.assertEqual(lib.git_state("/r/slow"), ("ok", True))

    def test_stale_entry_is_shown_at_once_and_refreshed_in_background(self):
        cache = {"/r/a": {"ts": time.time() - lib.GH_TTL - 5, "state": "diverged", "dirty": True}}
        self.cache_file().parent.mkdir(parents=True, exist_ok=True)
        self.cache_file().write_text(json.dumps(cache), encoding="utf-8")
        with mock.patch.object(lib, "_kick_refresh") as kick:
            self.assertEqual(lib.git_state("/r/a"), ("diverged", True))
            kick.assert_called_once_with()

    def test_fresh_entry_starts_no_round(self):
        cache = {"/r/a": {"ts": time.time(), "state": "ok", "dirty": False}}
        self.cache_file().parent.mkdir(parents=True, exist_ok=True)
        self.cache_file().write_text(json.dumps(cache), encoding="utf-8")
        with mock.patch.object(lib, "_kick_refresh") as kick:
            self.assertEqual(lib.git_state("/r/a"), ("ok", False))
            kick.assert_not_called()


class AtomicWriteTest(SharedGitStateTest):
    def test_failed_write_keeps_the_last_good_file(self):
        with mock.patch.object(lib, "_git_read", side_effect=self.fake_git_read), \
                mock.patch.object(lib, "_kick_refresh"):
            lib.refresh_git_states(["/r/a"])
            good = self.cache_file().read_text(encoding="utf-8")
            with mock.patch.object(lib.os, "replace", side_effect=OSError("disk full")):
                self.assertEqual(lib.refresh_git_states(["/r/b"]), 1)
        self.assertEqual(self.cache_file().read_text(encoding="utf-8"), good)
        self.assertEqual(sorted(json.loads(good)), ["/r/a"])
        leftovers = [p for p in self.cache_file().parent.iterdir() if p.name.endswith(".tmp")]
        self.assertEqual(leftovers, [])

    def test_cache_lives_in_the_state_folder_from_paths(self):
        with mock.patch.object(lib, "_git_read", side_effect=self.fake_git_read), \
                mock.patch.object(lib, "_kick_refresh"):
            lib.refresh_git_states(["/r/a"])
        self.assertTrue(self.cache_file().is_file())
        self.assertEqual(json.loads(self.cache_file().read_text(encoding="utf-8"))["/r/a"]["state"], "ok")


class RealGitRoundTest(SharedGitStateTest):
    def test_real_repo_reads_dirty_and_clean_through_the_cache(self):
        repo = self.root / "repo"
        repo.mkdir()
        env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@example.com",
                   GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@example.com")
        subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True, env=env)
        (repo / "README.md").write_text("init\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True, env=env)
        subprocess.run(["git", "-C", str(repo), "-c", "commit.gpgsign=false", "-c", "core.hooksPath=/dev/null",
                        "commit", "-q", "-m", "init"], check=True, env=env)
        fetch_dir = self.root / "fetch"
        with mock.patch.object(lib, "_FETCH_DIR", fetch_dir), mock.patch.object(lib, "_fetch_bg"), \
                mock.patch.object(lib, "_kick_refresh"):
            lib.refresh_git_states([str(repo)])
            self.assertEqual(lib.git_state(str(repo)), ("local", False))   # no remote: created, not pushed
            (repo / "new.txt").write_text("x\n", encoding="utf-8")
            self.age_cache(lib.GH_TTL + 1)
            lib.refresh_git_states([str(repo)])
            self.assertEqual(lib.git_state(str(repo)), ("local", True))


if __name__ == "__main__":
    unittest.main()
