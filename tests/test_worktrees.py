"""Tests for src/worktrees.py: create/list/prepare/merge/remove on a temporary git repo.

No network: every repo is created by the test. WS_WORKTREES points at a temporary root so the real
~/.local/share/ai-tmux-workspace/worktrees is never touched.
"""
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src"
sys.path.insert(0, str(SRC))

import worktrees  # noqa: E402


class WorktreeTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name).resolve()
        self.wtroot = self.root / "worktrees"
        old = {name: os.environ.get(name) for name in ("WS_WORKTREES", "AT_WS_CONFIG")}
        os.environ["WS_WORKTREES"] = str(self.wtroot)
        os.environ["AT_WS_CONFIG"] = str(self.root / "config.yaml")   # never read the real config

        def restore():
            for name, value in old.items():
                if value is None:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = value

        self.addCleanup(restore)
        self.repo = self.root / "myrepo"
        self.repo.mkdir()
        self.git("init", "-q")
        hooks = self.root / "no-hooks"          # keep the user's global commit-msg hooks out
        hooks.mkdir()
        self.git("config", "core.hooksPath", str(hooks))
        self.git("config", "user.name", "Test")
        self.git("config", "user.email", "test@example.com")
        self.git("config", "commit.gpgsign", "false")
        self.write("README.md", "init\n")
        self.commit("init")

    # helpers ---------------------------------------------------------------------------------
    def git(self, *args, cwd=None):
        return subprocess.run(["git", "-C", str(cwd or self.repo), *args],
                              capture_output=True, text=True, check=False)

    def git_ok(self, *args, cwd=None):
        run = self.git(*args, cwd=cwd)
        self.assertEqual(run.returncode, 0, run.stderr)
        return run.stdout.strip()

    def write(self, rel, text, repo=None):
        path = Path(repo or self.repo) / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def commit(self, message="change", repo=None):
        self.git_ok("add", "-A", cwd=repo)
        self.git_ok("commit", "-q", "-m", message, cwd=repo)

    def head(self, repo=None):
        return self.git_ok("rev-parse", "HEAD", cwd=repo)

    def branch_exists(self, branch):
        return self.git("rev-parse", "--verify", "--quiet", f"refs/heads/{branch}").returncode == 0

    # root outside the repos folder -----------------------------------------------------------
    def write_config(self, data):
        import yaml_min
        (self.root / "config.yaml").write_text(yaml_min.dumps(data), encoding="utf-8")

    def test_root_inside_the_repos_folder_is_refused_on_create(self):
        self.write_config({"repos_folder": str(self.root)})   # the root (worktrees/) sits inside it
        with self.assertRaises(worktrees.WorktreeError) as refused:
            worktrees.create(self.repo, "feature")
        self.assertIn(str(self.wtroot.resolve()), str(refused.exception))
        self.assertFalse(self.wtroot.exists())

    def test_root_of_the_folder_itself_is_refused_too(self):
        self.write_config({"repos_folder": str(self.wtroot)})
        with self.assertRaises(worktrees.WorktreeError):
            worktrees.create(self.repo, "feature")

    def test_root_outside_the_folder_is_accepted(self):
        folder = self.root / "Codes"
        folder.mkdir()
        self.write_config({"repos_folder": str(folder)})
        dest = worktrees.create(self.repo, "feature")
        self.assertTrue(dest.is_dir())

    def test_listing_the_windows_is_not_refused_by_the_folder_rule(self):
        self.write_config({"repos_folder": str(self.root)})
        self.assertEqual(worktrees.all_window_names(), [])

    # create / base --------------------------------------------------------------------------
    def test_create_makes_a_real_worktree_from_main(self):
        dest = worktrees.create(self.repo, "feature")
        self.assertEqual(dest, self.wtroot / "myrepo" / "feature")
        self.assertTrue(dest.is_dir())
        self.assertEqual(self.head(dest), self.head())
        self.assertEqual(self.git_ok("rev-parse", "--abbrev-ref", "HEAD", cwd=dest), "feature")
        listed = worktrees.list_worktrees(self.repo)
        self.assertIn({"path": str(dest), "branch": "feature", "head": self.head()},
                      [{k: e.get(k) for k in ("path", "branch", "head")} for e in listed])

    def test_base_falls_back_to_main_then_master(self):
        self.assertEqual(worktrees.default_base(self.repo), "main")
        self.git_ok("branch", "-m", "main", "master")
        self.assertEqual(worktrees.default_base(self.repo), "master")

    def test_origin_main_wins_over_main(self):
        origin = self.head()
        self.git_ok("update-ref", "refs/remotes/origin/main", origin)
        self.write("README.md", "newer\n")
        self.commit("newer main")
        self.assertNotEqual(self.head(), origin)
        self.assertEqual(worktrees.default_base(self.repo), "origin/main")
        dest = worktrees.create(self.repo, "from-origin")
        self.assertEqual(self.head(dest), origin)

    def test_explicit_and_config_base_override(self):
        first = self.head()
        self.write("README.md", "second\n")
        self.commit("second")
        self.write(".workspace.yaml", "base: main\n")
        self.commit("config")
        dest, _ = worktrees.new(self.repo, "from-config")
        self.assertEqual(self.head(dest), self.head())
        # an explicit base wins over the config
        dest2 = worktrees.create(self.repo, "from-explicit", base=first)
        self.assertEqual(self.head(dest2), first)

    def test_invalid_branches_are_refused(self):
        for branch in ("-x", "a b", "a..b", "", "a\tb", "/abs", "a//b", "a/./b", "a/.."):
            with self.subTest(branch=branch):
                with self.assertRaises(worktrees.WorktreeError):
                    worktrees.create(self.repo, branch)
        with self.assertRaises(worktrees.WorktreeError):
            worktrees.create(self.root / "not-a-repo", "feature")

    # list -----------------------------------------------------------------------------------
    def test_list_includes_main_and_every_worktree(self):
        one = worktrees.create(self.repo, "one")
        two = worktrees.create(self.repo, "two")
        entries = worktrees.list_worktrees(self.repo)
        pairs = {(e.get("branch"), e["path"]) for e in entries}
        self.assertIn(("main", str(self.repo)), pairs)
        self.assertIn(("one", str(one)), pairs)
        self.assertIn(("two", str(two)), pairs)

    # prepare --------------------------------------------------------------------------------
    def test_prepare_copies_includes_links_shared_and_runs_setup(self):
        self.write(".gitignore", ".env\nnode_modules/\n")
        self.write(".env", "SECRET=1\n")
        self.write("node_modules/pkg/keep.txt", "dep\n")
        self.write(".workspace.yaml", "\n".join([
            "include:",
            "  - .env",
            "shared:",
            "  - node_modules",
            "setup: printf hi > setup.txt",
        ]) + "\n")
        self.commit("workspace config")

        dest, result = worktrees.new(self.repo, "prepared")

        self.assertEqual((dest / ".env").read_text(encoding="utf-8"), "SECRET=1\n")
        self.assertTrue((dest / "node_modules").is_symlink())
        self.assertEqual((dest / "node_modules").resolve(), (self.repo / "node_modules").resolve())
        self.assertEqual((dest / "setup.txt").read_text(encoding="utf-8"), "hi")
        self.assertEqual(result["included"], [".env"])
        self.assertEqual(result["shared"], ["node_modules"])
        self.assertTrue(result["setup"])

    def test_prepare_ignores_paths_outside_the_repo(self):
        outside = self.root / "outside.txt"
        outside.write_text("secret\n", encoding="utf-8")
        (self.root / "outside-dir").mkdir()
        self.write(".workspace.yaml", "include:\n  - ../outside.txt\nshared:\n  - ../outside-dir\n")
        self.commit("workspace config")
        dest, result = worktrees.new(self.repo, "traversal")
        self.assertEqual(result["included"], [])
        self.assertEqual(result["shared"], [])
        self.assertTrue(dest.is_dir())

    def test_prepare_include_only_copies_gitignored_files(self):
        self.write("tracked.txt", "tracked\n")
        self.write(".workspace.yaml", "include:\n  - tracked.txt\n")
        self.commit("tracked include")
        dest = worktrees.create(self.repo, "tracked-include")
        result = worktrees.prepare(self.repo, dest)
        self.assertEqual(result["included"], [])

    def test_missing_config_means_no_preparation(self):
        dest = worktrees.create(self.repo, "plain")
        result = worktrees.prepare(self.repo, dest)
        self.assertEqual(result, {"included": [], "shared": [], "setup": False})
        self.assertEqual(sorted(p.name for p in dest.iterdir() if p.name != ".git"), ["README.md"])

    def test_setup_failure_reports_stderr(self):
        self.write(".workspace.yaml", 'setup: "echo boom >&2; exit 3"\n')
        self.commit("workspace config")
        dest = worktrees.create(self.repo, "broken-setup")
        with self.assertRaises(worktrees.WorktreeError) as caught:
            worktrees.prepare(self.repo, dest)
        self.assertIn("boom", str(caught.exception))

    # merge ----------------------------------------------------------------------------------
    def test_merge_brings_the_branch_into_the_main_checkout(self):
        dest = worktrees.create(self.repo, "feature")
        self.write("feature.txt", "from the branch\n", repo=dest)
        self.commit("feature work", repo=dest)
        worktrees.merge(self.repo, "feature")
        self.assertEqual((self.repo / "feature.txt").read_text(encoding="utf-8"), "from the branch\n")
        self.assertEqual(self.git_ok("rev-parse", "--abbrev-ref", "HEAD"), "main")
        self.assertTrue(self.git_ok("log", "-1", "--format=%s").startswith("Merge"))

    def test_conflict_stops_and_leaves_everything_in_place(self):
        dest = worktrees.create(self.repo, "conflict")
        self.write("README.md", "branch\n", repo=dest)
        self.commit("branch side", repo=dest)
        self.write("README.md", "main\n")
        self.commit("main side")

        with self.assertRaises(worktrees.MergeConflict) as caught:
            worktrees.merge(self.repo, "conflict")

        self.assertIn("CONFLICT", caught.exception.stderr)
        self.assertTrue(dest.is_dir())
        self.assertEqual(self.git("rev-parse", "-q", "--verify", "MERGE_HEAD").returncode, 0)
        self.assertTrue(self.branch_exists("conflict"))

    # remove ---------------------------------------------------------------------------------
    def test_remove_clean_merged_worktree_deletes_the_branch(self):
        dest = worktrees.create(self.repo, "merged")
        result = worktrees.remove(self.repo, "merged")
        self.assertFalse(dest.exists())
        self.assertTrue(result["merged"])
        self.assertTrue(result["branch_removed"])
        self.assertFalse(self.branch_exists("merged"))

    def test_remove_refuses_a_dirty_worktree(self):
        dest = worktrees.create(self.repo, "dirty")
        self.write("README.md", "changed without commit\n", repo=dest)
        with self.assertRaises(worktrees.WorktreeError):
            worktrees.remove(self.repo, "dirty")
        self.assertTrue(dest.is_dir())
        self.assertTrue(self.branch_exists("dirty"))

    def test_remove_preserves_an_unmerged_branch(self):
        dest = worktrees.create(self.repo, "unmerged")
        self.write("only-here.txt", "not merged\n", repo=dest)
        self.commit("unmerged work", repo=dest)
        result = worktrees.remove(self.repo, "unmerged")
        self.assertFalse(dest.exists())
        self.assertFalse(result["merged"])
        self.assertFalse(result["branch_removed"])
        self.assertTrue(self.branch_exists("unmerged"))

    # prune support ---------------------------------------------------------------------------
    def test_window_names_include_worktrees(self):
        self.assertEqual(worktrees.all_window_names(), [])
        alpha = worktrees.create(self.repo, "alpha")
        beta = worktrees.create(self.repo, "beta")
        self.assertTrue(alpha.is_dir() and beta.is_dir())
        self.assertEqual(sorted(worktrees.all_window_names()), ["alpha", "beta"])


class CliTest(unittest.TestCase):
    def test_usage_without_arguments(self):
        import contextlib
        import io
        for argv in ([], ["list"], ["remove", "repo"], ["merge", "repo"], ["nope"]):
            with self.subTest(argv=argv):
                with contextlib.redirect_stderr(io.StringIO()):
                    self.assertEqual(worktrees.main(argv), 2)


if __name__ == "__main__":
    unittest.main()
