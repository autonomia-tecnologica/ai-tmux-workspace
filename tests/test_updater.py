"""`workspace update` (src/updater.py): show, --apply, --rollback, and the checks around them.

Everything runs under a temporary HOME with a local git remote and TMUX_TMPDIR pointing at an empty
folder, so no test touches the real ~/.local/share/ai-tmux-workspace, ~/.config or a tmux server.
"""
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BASH = shutil.which("bash") or "/bin/bash"
SRC = ROOT / "src"
sys.path.insert(0, str(SRC))

import i18n  # noqa: E402
import updater  # noqa: E402

SKIP_COPY = shutil.ignore_patterns(".git", ".claude", "__pycache__")


def git(cwd, *args):
    # core.hooksPath=/dev/null: the test repos ignore the machine's global hooks; user.* keeps commits
    # independent of the global git config.
    return subprocess.run(["git", "-c", "user.name=test", "-c", "user.email=test@example.invalid",
                           "-c", "core.hooksPath=/dev/null", *args],
                          cwd=str(cwd), capture_output=True, text=True, check=True).stdout.strip()


def tree_bytes(root):
    """relative path -> bytes of every file under `root`, except the source.json record of the installed copy."""
    root = Path(root)
    found = {}
    for path in sorted(root.rglob("*")):
        if path.is_file() and path.name != "source.json" and "__pycache__" not in path.parts:
            found[str(path.relative_to(root))] = path.read_bytes()
    return found


class UpdateTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name).resolve()
        self.home = self.root / "home"
        self.config = self.home / "cfg" / "config.yaml"
        self.tmux_tmp = self.root / "tmux"
        self.stubs = self.root / "bin"
        for directory in (self.home, self.config.parent, self.tmux_tmp, self.stubs):
            directory.mkdir(parents=True)
        claude = self.stubs / "claude"
        claude.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        claude.chmod(0o755)
        self.share = self.home / ".local" / "share" / "ai-tmux-workspace"
        self.clone = self.share / "app"
        self.installed_harness = self.share / "harness"

        self.remote = self.root / "remote"
        shutil.copytree(ROOT, self.remote, ignore=SKIP_COPY)
        git(self.remote, "init", "-q", "-b", "main")
        git(self.remote, "add", "-A")
        git(self.remote, "commit", "-q", "-m", "initial")
        self.share.mkdir(parents=True)
        git(self.root, "clone", "-q", "--depth", "1", self.remote.as_uri(), str(self.clone))
        shutil.copy2(ROOT / "config" / "config.example.yaml", self.config)
        # The first install of the harness and hooks, as the installer leaves them.
        first = self.run_workspace(["config", "update", "--apply"])
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)

    # --- helpers ----------------------------------------------------------------------------------
    def env(self, **overrides):
        env = {key: value for key, value in os.environ.items()
               if not key.startswith(("AT_", "WS_")) and key not in ("TMUX", "TMUX_TMPDIR")}
        env.update(HOME=str(self.home), AT_WS_CONFIG=str(self.config), TMUX_TMPDIR=str(self.tmux_tmp),
                   AT_WS_REPO_URL=self.remote.as_uri(), NO_COLOR="1", AT_WS_NO_TTY="1",
                   PATH=f"{self.stubs}{os.pathsep}{os.environ.get('PATH', '')}")
        env.update(overrides)
        return env

    def run_workspace(self, argv, script=None, **overrides):
        """bin/workspace with `argv` (never without one: a bare call would open tmux)."""
        script = script or self.clone / "bin" / "workspace"
        return subprocess.run([BASH, str(script), *argv], env=self.env(**overrides),
                              capture_output=True, text=True, check=False, timeout=300)

    def run_update(self, args, script=None, **overrides):
        return self.run_workspace(["update", *args], script=script, **overrides)

    def remote_commit(self, files, message="next"):
        for relative, text in files.items():
            target = self.remote / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text, encoding="utf-8")
        git(self.remote, "add", "-A")
        git(self.remote, "commit", "-q", "-m", message)
        return git(self.remote, "rev-parse", "HEAD")

    def head(self, cwd=None):
        return git(cwd or self.clone, "rev-parse", "HEAD")

    def record(self):
        return self.config.parent / "state" / "update-previous.json"

    # --- show -------------------------------------------------------------------------------------
    def test_show_reports_versions_commits_and_changes_and_changes_nothing(self):
        before = self.head()
        self.remote_commit({"harness/README.md": "harness novo\n", "NOVO.md": "novo\n"}, "traz harness novo")
        harness_before = tree_bytes(self.installed_harness)
        result = self.run_update([])
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("versão instalada", result.stdout)
        self.assertIn("versão remota", result.stdout)
        self.assertIn("traz harness novo", result.stdout)
        self.assertIn("harness: harness/README.md", result.stdout)
        self.assertIn("Nada foi alterado", result.stdout)
        self.assertEqual(self.head(), before)
        self.assertEqual(tree_bytes(self.installed_harness), harness_before)
        self.assertFalse(self.record().exists())

    def test_show_on_an_up_to_date_clone_says_so(self):
        result = self.run_update([])
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn(i18n.t("update.uptodate"), result.stdout)

    def test_unknown_argument_is_a_usage_error(self):
        result = self.run_update(["--bogus"])
        self.assertEqual(result.returncode, 2)
        self.assertIn(i18n.t("update.usage"), result.stderr)

    # --- apply ------------------------------------------------------------------------------------
    def assertRepoHarnessInstalled(self):
        installed = tree_bytes(self.installed_harness)
        for relative, data in tree_bytes(self.clone / "harness").items():
            with self.subTest(file=relative):
                self.assertEqual(installed.get(relative), data)

    def test_apply_fast_forwards_syncs_the_harness_and_keeps_the_previous_revision(self):
        before = self.head()
        # forcado.yaml comes from another source than the repo: the update must not remove it.
        (self.installed_harness / "forcado.yaml").write_text("versao: 1\n", encoding="utf-8")
        new = self.remote_commit({"harness/README.md": "harness novo\n", "NOVO.md": "novo\n"})
        result = self.run_update(["--apply"])
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.head(), new)
        self.assertTrue((self.clone / "NOVO.md").is_file())
        self.assertIn("ok: clone em", result.stdout)
        self.assertIn("ok: config, harness, settings e hooks em dia", result.stdout)
        self.assertIn("pronto", result.stdout)
        self.assertNotIn("hooks ou plugins mudaram", result.stdout)
        # Every file of the repo's harness/ (regras/ included) is byte-identical in the installed copy.
        self.assertRepoHarnessInstalled()
        # Files the repo does not ship stay where they are: nothing is removed.
        self.assertEqual((self.installed_harness / "forcado.yaml").read_text(encoding="utf-8"), "versao: 1\n")
        self.assertEqual(
            (self.installed_harness / "README.md").read_text(encoding="utf-8"), "harness novo\n")
        record = self.record().read_text(encoding="utf-8")
        self.assertIn(before, record)
        self.assertIn(new, record)

    def test_apply_is_idempotent(self):
        self.remote_commit({"NOVO.md": "novo\n"})
        first = self.run_update(["--apply"])
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        record = self.record().read_text(encoding="utf-8")
        second = self.run_update(["--apply"])
        self.assertEqual(second.returncode, 0, second.stdout + second.stderr)
        self.assertIn(i18n.t("update.uptodate"), second.stdout)
        self.assertEqual(self.record().read_text(encoding="utf-8"), record)

    def test_a_hook_change_asks_for_restart(self):
        self.remote_commit({"src/hook.py": "# hook novo\n"})
        result = self.run_update(["--apply"])
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn(i18n.t("update.restart"), result.stdout)

    def test_dirty_clone_aborts_and_changes_nothing(self):
        before = self.head()
        self.remote_commit({"NOVO.md": "novo\n"})
        (self.clone / "README.md").write_text("local edit\n", encoding="utf-8")
        result = self.run_update(["--apply"])
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(i18n.t("cli.update.dirty", root=str(self.clone)), result.stderr)
        self.assertEqual(self.head(), before)
        self.assertFalse((self.clone / "NOVO.md").exists())
        self.assertEqual((self.clone / "README.md").read_text(encoding="utf-8"), "local edit\n")

    def test_unreachable_remote_fails_clearly_and_changes_nothing(self):
        before = self.head()
        harness_before = tree_bytes(self.installed_harness)
        gone = (self.root / "sumiu").as_uri()
        git(self.clone, "remote", "set-url", "origin", gone)
        for args in ([], ["--apply"]):
            with self.subTest(args=args):
                result = self.run_update(args, AT_WS_REPO_URL=gone)
                self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                self.assertIn("não consegui buscar o remoto", result.stderr)
                self.assertEqual(self.head(), before)
                self.assertEqual(tree_bytes(self.installed_harness), harness_before)
                self.assertFalse(self.record().exists())

    def test_unexpected_remote_aborts(self):
        before = self.head()
        result = self.run_update(["--apply"], AT_WS_REPO_URL=(self.root / "outro").as_uri())
        self.assertEqual(result.returncode, 1)
        self.assertIn("o remoto do clone é", result.stderr)
        self.assertEqual(self.head(), before)

    def test_divergent_history_aborts_without_a_merge(self):
        before = git(self.clone, "rev-parse", "HEAD")
        git(self.clone, "commit", "-q", "--allow-empty", "-m", "so local")
        local = self.head()
        self.remote_commit({"NOVO.md": "novo\n"})
        result = self.run_update(["--apply"])
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("divergiu", result.stderr)
        self.assertEqual(self.head(), local)
        self.assertNotEqual(self.head(), before)
        self.assertFalse((self.clone / "NOVO.md").exists())

    def test_a_failing_step_undoes_the_whole_update(self):
        before = self.head()
        harness_before = tree_bytes(self.installed_harness)
        self.remote_commit({"harness/README.md": "harness novo\n", "NOVO.md": "novo\n"})
        # config update reads this registry; a damaged one fails the config step after the harness copy.
        offered = self.config.parent / "state" / "config-offered.json"
        offered.parent.mkdir(parents=True, exist_ok=True)
        offered.write_text("isto nao e json", encoding="utf-8")
        result = self.run_update(["--apply"])
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn(i18n.t("update.config.failed"), result.stdout)
        self.assertIn(i18n.t("update.undone.rev", rev=before[:7]), result.stdout)
        self.assertEqual(self.head(), before)
        self.assertFalse((self.clone / "NOVO.md").exists())
        self.assertEqual(tree_bytes(self.installed_harness), harness_before)
        self.assertEqual(offered.read_text(encoding="utf-8"), "isto nao e json")
        self.assertFalse(self.record().exists())

    # --- rollback ---------------------------------------------------------------------------------
    def test_rollback_returns_to_the_previous_revision_and_its_harness(self):
        before = self.head()
        harness_before = tree_bytes(self.installed_harness)
        self.remote_commit({"harness/README.md": "harness novo\n", "NOVO.md": "novo\n"})
        applied = self.run_update(["--apply"])
        self.assertEqual(applied.returncode, 0, applied.stdout + applied.stderr)
        rolled = self.run_update(["--rollback"])
        self.assertEqual(rolled.returncode, 0, rolled.stdout + rolled.stderr)
        self.assertEqual(self.head(), before)
        self.assertFalse((self.clone / "NOVO.md").exists())
        self.assertEqual(tree_bytes(self.installed_harness), harness_before)
        self.assertFalse(self.record().exists())
        again = self.run_update(["--rollback"])
        self.assertEqual(again.returncode, 1)
        self.assertIn(i18n.t("update.rollback.none"), again.stderr)

    def test_rollback_refuses_when_the_clone_moved_after_the_update(self):
        self.remote_commit({"NOVO.md": "novo\n"})
        self.assertEqual(self.run_update(["--apply"]).returncode, 0)
        git(self.clone, "commit", "-q", "--allow-empty", "-m", "depois")
        moved = self.head()
        result = self.run_update(["--rollback"])
        self.assertEqual(result.returncode, 1)
        self.assertIn("não voltei nada", result.stderr)
        self.assertEqual(self.head(), moved)
        self.assertTrue(self.record().exists())

    # --- dev checkout -----------------------------------------------------------------------------
    def test_dev_checkout_only_carries_harness_and_config_without_git(self):
        dev = self.root / "dev"
        shutil.copytree(ROOT, dev, ignore=SKIP_COPY)
        (dev / "harness" / "README.md").write_text("harness de dev\n", encoding="utf-8")
        clone_head = self.head()
        shown = self.run_update([], script=dev / "bin" / "workspace")
        self.assertEqual(shown.returncode, 0, shown.stdout + shown.stderr)
        self.assertIn("checkout de desenvolvimento", shown.stdout)
        applied = self.run_update(["--apply"], script=dev / "bin" / "workspace")
        self.assertEqual(applied.returncode, 0, applied.stdout + applied.stderr)
        self.assertIn("checkout de desenvolvimento", applied.stdout)
        self.assertEqual((self.installed_harness / "README.md").read_text(encoding="utf-8"), "harness de dev\n")
        self.assertEqual(self.head(), clone_head)
        self.assertFalse(self.record().exists())

    def test_rollback_is_refused_on_a_dev_checkout(self):
        dev = self.root / "dev"
        shutil.copytree(ROOT, dev, ignore=SKIP_COPY)
        result = self.run_update(["--rollback"], script=dev / "bin" / "workspace")
        self.assertEqual(result.returncode, 1)
        self.assertIn(i18n.t("update.rollback.dev"), result.stderr)


class UpdaterHelpersTest(unittest.TestCase):
    def test_remote_comparison_ignores_a_trailing_slash_and_dot_git(self):
        self.assertTrue(updater.same_remote("https://host/org/repo.git/", "https://host/org/repo"))
        self.assertFalse(updater.same_remote("https://host/org/other", "https://host/org/repo"))

    def test_changed_files_are_grouped_by_what_they_change(self):
        groups = updater.group_changes([
            "config/config.example.yaml", "harness/HARNESS.md", "security/policy.yaml",
            "src/hook.py", "security/opencode-policy.js", "src/lib.py",
        ])
        self.assertEqual(groups["config"], ["config/config.example.yaml"])
        self.assertEqual(groups["harness"], ["harness/HARNESS.md"])
        self.assertEqual(groups["settings"], ["security/policy.yaml"])
        self.assertEqual(groups["hooks"], ["src/hook.py", "security/opencode-policy.js"])
        self.assertEqual(groups["code"], ["src/lib.py"])


if __name__ == "__main__":
    unittest.main()
