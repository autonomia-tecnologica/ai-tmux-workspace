"""Tests for the per-repo agent command state that decides how a repo's Claude pane opens: the same command
keeps `--continue` (today's flow); a different command, an unknown repo, or a last opening that never produced
a transcript opens a new session and, when a previous transcript exists, passes the i18n migration prompt as
the positional argument of `claude`. Each test runs `src/lib.py` in a child process with temporary HOME,
WS_ESTADO and AT_WS_CONFIG, so the real config, cache and transcripts are never read or written."""
import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LIB = ROOT / "src" / "lib.py"


class OpeningCommandTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name).resolve()
        self.home = self.root / "home"
        self.state = self.root / "state"
        self.home.mkdir()
        self.state.mkdir()
        self.config = self.root / "config.json"
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.set_command("claude --settings ~/.claude/settings.a.json")

    def set_command(self, command, agent="a"):
        self.agent = agent
        self.config.write_text(json.dumps({"agent": agent, "profiles": {agent: command}}), encoding="utf-8")
        self.command = command

    @property
    def launch(self):
        """Command line the workspace actually runs: every profile gets the workspace-generated
        settings (`--settings`), except a legacy string command that already passes its own; non-Claude
        profiles disable tool search (D9) and open in bypassPermissions, since the classifier does not
        run there; the claude profile opens in acceptEdits (D10)."""
        cmd = self.command
        if "--settings" not in cmd:
            cmd = f"{cmd} --settings {shlex.quote(str(self.state / 'settings' / f'{self.agent}.json'))}"
        base = cmd if self.agent == "claude" else f"env ENABLE_TOOL_SEARCH=false {cmd}"
        mode = "acceptEdits" if self.agent == "claude" else "bypassPermissions"
        return f"{base} --permission-mode {mode}"

    def opening(self, repo=None):
        env = dict(os.environ, HOME=str(self.home), WS_ESTADO=str(self.state), AT_WS_CONFIG=str(self.config),
                   AT_HARNESS_DIR=str(self.root / "no-harness"))
        proc = subprocess.run([sys.executable, str(LIB), "opening-command", str(repo or self.repo)],
                              env=env, capture_output=True, text=True, check=False)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return proc.stdout.strip()

    def transcript(self, repo=None, name="old.jsonl", mtime=None):
        folder = self.home / ".claude" / "projects" / re.sub(r"[^A-Za-z0-9]", "-", str(repo or self.repo))
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / name
        path.write_text('{"type": "user"}\n', encoding="utf-8")
        if mtime is not None:
            os.utime(path, (mtime, mtime))
        return path

    def new_session_transcript(self, repo=None, name="new.jsonl"):
        """Transcript a successful opening would leave: newer than the state recorded before it."""
        return self.transcript(repo, name=name, mtime=time.time() + 60)

    def saved(self):
        return json.loads((self.state / "agent.json").read_text(encoding="utf-8"))

    def assert_prompt(self, out, transcript):
        self.assertNotIn("--continue", out)
        argv = shlex.split(out)
        base = shlex.split(self.launch)
        # "<agent command> <prompt> || <agent command>": one positional argument, the prompt
        self.assertEqual(argv[:len(base)], base)
        self.assertEqual(argv[len(base) + 1:], ["||"] + base)
        self.assertEqual(len(argv), 2 * len(base) + 2)
        prompt = argv[len(base)]
        self.assertIn(str(transcript), prompt)
        self.assertIn("migrado", prompt)
        self.assertIn("releia a transcrição", prompt.lower())

    def test_first_opening_without_record_does_not_resume(self):
        self.assertEqual(self.opening(), self.launch)

    def test_non_claude_profile_disables_tool_search(self):
        out = self.opening()
        self.assertTrue(out.startswith("env ENABLE_TOOL_SEARCH=false "))
        self.assertIn(self.command, out)

    def test_claude_profile_keeps_tool_search(self):
        self.set_command("claude", agent="claude")
        self.assertEqual(self.opening(), self.launch)

    def test_tool_search_prefix_is_not_part_of_the_recorded_identity(self):
        self.opening()
        self.assertEqual(self.saved()[str(self.repo)]["command"], self.command)
        self.new_session_transcript()
        self.assertEqual(self.opening(), f"{self.launch} --continue || {self.launch}")

    def test_first_opening_with_a_transcript_gets_the_migration_prompt(self):
        old = self.transcript()
        self.set_command("claude --settings ~/.claude/settings.b.json")
        self.assert_prompt(self.opening(), old)

    def test_same_command_after_restart_or_reload_keeps_continue(self):
        self.opening()                          # records command A, no session yet
        self.new_session_transcript()           # the session this opening started
        out = self.opening()
        self.assertEqual(out, f"{self.launch} --continue || {self.launch}")

    def test_prompt_is_shown_only_once_per_change(self):
        self.opening()
        old = self.transcript()
        self.set_command("claude --settings ~/.claude/settings.b.json")
        self.assert_prompt(self.opening(), old)             # first opening with the new command
        again = self.opening()
        self.assertNotIn("migrado", again)
        self.assertEqual(again, f"{self.launch} --continue || {self.launch}")

    def test_prompt_is_shown_once_across_repos(self):
        second = self.root / "second"
        second.mkdir()
        self.opening()                          # record command A for both repos (no transcripts yet)
        self.opening(second)
        old = self.transcript()
        self.transcript(second)
        self.set_command("claude --settings ~/.claude/settings.b.json")
        self.assert_prompt(self.opening(), old)             # first repo of the new environment
        out = self.opening(second)
        self.assertEqual(out, self.launch)                  # new session, prompt not repeated
        self.assertNotIn("migrado", out)

    def test_old_string_state_format_is_understood(self):
        (self.state / "agent.json").write_text(json.dumps({str(self.repo): self.command}), encoding="utf-8")
        self.assertEqual(self.opening(), f"{self.launch} --continue || {self.launch}")

    def test_changed_command_opens_new_session_with_prompt_citing_the_transcript(self):
        self.opening()
        old = self.transcript()
        self.set_command("claude --settings ~/.claude/settings.b.json")
        self.assert_prompt(self.opening(), old)

    def test_second_opening_with_the_new_command_continues(self):
        self.opening()
        self.transcript()                       # transcript from the old environment
        self.set_command("claude --settings ~/.claude/settings.b.json")
        self.assertNotIn("--continue", self.opening())
        self.new_session_transcript()           # the new session this opening started
        self.assertEqual(self.opening(), f"{self.launch} --continue || {self.launch}")
        self.assertEqual(self.saved()[str(self.repo)]["command"], self.command)

    def test_repo_without_transcript_opens_new_session_without_prompt(self):
        other = self.root / "other"
        other.mkdir()
        self.opening(other)                     # last opening of `other` used command A
        self.set_command("claude --settings ~/.claude/settings.b.json")
        self.assertEqual(self.opening(other), self.launch)

    def test_newest_transcript_in_the_project_folder_wins(self):
        self.transcript(name="old.jsonl", mtime=1000)
        new = self.transcript(name="new.jsonl", mtime=2000)
        self.set_command("claude --settings ~/.claude/settings.b.json")
        out = self.opening()
        self.assertIn(str(new), out)
        self.assertNotIn("old.jsonl", out)

    def test_transcript_fallback_from_the_session_cache(self):
        transcript = self.root / "cached.jsonl"
        transcript.write_text("{}\n", encoding="utf-8")
        sessions = self.state / "sessoes"
        sessions.mkdir(parents=True)
        (sessions / "abc.json").write_text(json.dumps({
            "cwd": str(self.repo), "ts": 1, "transcript_path": str(transcript)}), encoding="utf-8")
        self.set_command("claude --settings ~/.claude/settings.b.json")
        out = self.opening()
        self.assertIn(str(transcript), out)
        self.assertNotIn("--continue", out)

    def test_cache_entry_of_another_repo_is_not_used(self):
        transcript = self.root / "elsewhere.jsonl"
        transcript.write_text("{}\n", encoding="utf-8")
        sessions = self.state / "sessoes"
        sessions.mkdir(parents=True)
        (sessions / "abc.json").write_text(json.dumps({
            "cwd": str(self.root / "elsewhere"), "ts": 1, "transcript_path": str(transcript)}),
            encoding="utf-8")
        self.set_command("claude --settings ~/.claude/settings.b.json")
        self.assertEqual(self.opening(), self.launch)


if __name__ == "__main__":
    unittest.main()
