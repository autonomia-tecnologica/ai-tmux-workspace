"""Regression tests for read-only Bash commands that the policy denied by mistake (backlog item 158).

Each reported form is a test with its exact command. Every fixed form has a write pair that must
stay denied: a write into the control plane, into `~/.claude`, a secret or outside the box is still a
deny. A form that remains denied on purpose (an ambiguous form that names a control path after `cd`)
is not in this file.

Every test uses a temporary HOME, state directory and repository, as `test_policy.py` does.
"""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import sys

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import policy  # noqa: E402

ENV_KEYS = (
    "HOME", "WS_STATE", "WS_SESSION_ROLE", "WS_SESSION_ROOT", "WS_SESSION_PARENT_ROLE",
    "WS_PARENT_ROLE", "WS_SESSION_ID", "WS_POLICY", "WS_POLICY_AUTO", "WS_AUTO", "WS_DIR",
    "WS_I18N", "WS_POLICY_CLI", "TMPDIR", "WS_POLICY_ASK", "WS_POLICY_OPEN", "AT_WS_CONFIG",
)


class ReadFalsePositivesTest(unittest.TestCase):
    def setUp(self):
        # Not under /tmp: /tmp is inside every box, so a temp repo or home there would mask the
        # outside-the-box cases (see test_policy.py).
        base = "/var/tmp" if os.access("/var/tmp", os.W_OK) else None
        tmp = tempfile.TemporaryDirectory(dir=base)
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        self.home = self.root / "home"
        self.repo = self.root / "repo"
        self.state = self.root / "state"
        self.control = self.home / ".config" / "ai-tmux-workspace"
        for folder in (self.control / "state", self.home / ".claude" / "hooks", self.repo, self.state):
            folder.mkdir(parents=True, exist_ok=True)
        env = mock.patch.dict(os.environ)
        env.start()
        self.addCleanup(env.stop)
        for name in ENV_KEYS:
            os.environ.pop(name, None)
        os.environ.update(HOME=str(self.home), WS_STATE=str(self.state),
                          WS_SESSION_ROLE="repo", WS_SESSION_ROOT=str(self.repo))

    def bash(self, command):
        return {"tool_name": "Bash", "tool_input": {"command": command}, "session_id": "sess-1"}

    def decide(self, command):
        return policy.check_event(self.bash(command))

    def assertDecision(self, command, decision, capability=None):
        result = self.decide(command)
        with self.subTest(command=command):
            self.assertEqual((result["decision"], result["capability"]), (decision, capability))

    def assertRead(self, command):
        """Allowed, with no capability: the program read something that the box allows."""
        result = self.decide(command)
        with self.subTest(command=command):
            self.assertEqual(result["decision"], "allow")
            self.assertNotIn(result["capability"], ("repo.outside", "secrets", "control_plane.write",
                                                    "config.write", "workspace.write"))

    # --- (a) a program name after `cd` is not a path -------------------------------------------
    def test_program_after_cd_into_the_control_plane_is_not_a_path(self):
        # `grep` is the program, not a file named `grep` in the `cd` folder. Before the fix it was
        # resolved there and denied as control_plane.write.
        for command in (
            f"cd {self.control}/state && grep -r",
        ):
            with self.subTest(command=command):
                result = self.decide(command)
                self.assertEqual((result["decision"], result["reason"]),
                                 ("allow", "policy.reason.defer"))

    def test_python_code_after_cd_into_the_control_plane_reads_without_a_write_verdict(self):
        # The exact form of item 158 (a): the code of `python3 -c` is code, not a path.
        result = self.decide(f"cd {self.control}/state && tail -n 5 policy.jsonl | "
                             "python3 -I -c 'import sys; print(1)'")
        self.assertEqual(result["decision"], "allow")
        # Approved scope (coordinator, item 158): a bare word inside `python -c` code is not a
        # path, so a relative name in that code is not a control-plane write verdict either.
        self.assertNotEqual(self.decide(
            f"cd {self.control}/state && python3 -I -c 'open(\"f.txt\",\"w\")'")["capability"],
            "control_plane.write")

    def test_explicit_paths_in_python_code_and_shell_writes_stay_denied(self):
        # An explicit path (`/`, `~`, `.`) inside the code still meets the barrier, and every write
        # form of the shell stays denied after the same `cd`.
        for command in (
            f"cd {self.control}/state && python3 -I -c 'open(\"{self.control}/state/policy.jsonl\",\"w\")'",
            f"cd {self.control}/state && python3 -I -c 'open(\"./policy.jsonl\",\"w\")'",
            f"cd {self.control}/state && python3 -I -c 'open(\"~/.config/ai-tmux-workspace/state/x\",\"w\")'",
            f"cd {self.control}/state && python3 -c 'print(1)' > policy.jsonl",
            f"cd {self.control}/state && python3 -c 'print(1)' ; tee f",
        ):
            self.assertDecision(command, "deny", "control_plane.write")

    def test_writes_into_the_control_plane_after_cd_stay_denied(self):
        # Pairs of the case above: a real write after the same `cd` is still control_plane.write.
        for command in (
            f"cd {self.control}/state && tee f",
            f"cd {self.control}/state && sh -c 'touch f'",
            f"cd {self.control}/state && rm -rf policy.jsonl",
            f"cd {self.control}/state && echo x > policy.jsonl",
            f"cd {self.control}/state && cp /tmp/x policy.jsonl",
        ):
            self.assertDecision(command, "deny", "control_plane.write")

    # --- (b) a glob is a secret only when it names a credential file ---------------------------
    def test_extension_globs_that_are_not_credentials_are_not_secrets(self):
        for command in ("cat *.json", "ls *.json", "cat *.js", "ls *.js"):
            with self.subTest(command=command):
                result = self.decide(command)
                self.assertEqual((result["decision"], result["reason"]),
                                 ("allow", "policy.reason.defer"))

    def test_credential_globs_stay_secrets_and_their_writes_too(self):
        for command in ("cat .env*", "cat *.env", "cat *.pem", "cat id_*", "cat *.key"):
            self.assertDecision(command, "deny", "secrets")
        for command in ("rm .env*", "echo x > .env", "tee *.pem"):
            self.assertDecision(command, "deny", "secrets")

    # --- (c) `ls` reads are not outside reads -------------------------------------------------
    def test_ls_forms_from_the_reports_are_allowed(self):
        self.assertRead("ls")
        result = self.decide("ls *.json")
        self.assertEqual((result["decision"], result["reason"]), ("allow", "policy.reason.defer"))
        self.assertDecision("ls /", "allow", None)
        self.assertDecision("ls -la /", "allow", None)

    # --- (d) `/` and the standard streams are not outside reads -------------------------------
    def test_root_and_standard_device_reads_are_allowed(self):
        for command in ("cat /dev/null", "cat /dev/stdout", "cat /dev/stderr", "ls /", "ls -la /"):
            result = self.decide(command)
            with self.subTest(command=command):
                self.assertEqual((result["decision"], result["reason"]), ("allow", "policy.reason.inert"))

    def test_other_outside_reads_and_writes_stay_denied(self):
        self.assertDecision("cat /dev/zero", "deny", "repo.outside")
        self.assertDecision("cat /etc/passwd", "deny", "repo.outside")
        self.assertDecision("rm -rf /", "deny", "repo.outside")
        # `/` is neutral only for a non-recursive read: a walk of the tree still reaches outside.
        self.assertDecision("find / -name id_rsa", "deny", "repo.outside")
        self.assertDecision("ls -R /", "deny", "repo.outside")
        self.assertDecision("tree /", "deny", "repo.outside")

    # --- (e) reading a hook script under ~/.claude is a read; writing it is still a deny ------
    def test_copy_source_under_claude_is_a_read_and_destination_stays_a_write(self):
        self.assertDecision(
            f"cd {self.home} && cp .claude/hooks/godot-capture-sandbox.sh "
            f"{self.repo}/godot-capture-sandbox.sh", "allow", "config.read")
        self.assertDecision(
            f"cd {self.home} && cp -p .claude/hooks/godot-capture-sandbox.sh /tmp/x.sh",
            "allow", "config.read")
        self.assertDecision(
            f"cd {self.home} && cat .claude/hooks/godot-capture-sandbox.sh", "allow", "config.read")

    def test_writes_into_claude_stay_denied_including_copy_and_move_of_the_hook(self):
        for command in (
            f"cd {self.home} && cp {self.repo}/x.sh .claude/hooks/godot-capture-sandbox.sh",
            f"cd {self.home} && cp -t .claude/hooks {self.repo}/x.sh",
            f"cd {self.home} && mv .claude/hooks/godot-capture-sandbox.sh {self.repo}/x.sh",
            f"cd {self.home} && rm .claude/hooks/godot-capture-sandbox.sh",
            f"cd {self.home} && echo x > .claude/hooks/godot-capture-sandbox.sh",
        ):
            self.assertDecision(command, "deny", "config.write")
        # `chmod` mode operands are read as paths (not fixed here): the decision is still a deny.
        self.assertEqual(self.decide(
            f"cd {self.home} && chmod 755 .claude/hooks/godot-capture-sandbox.sh")["decision"], "deny")

    def test_copy_sources_keep_their_protected_decisions(self):
        # A copy from a secret, or from the control plane, is still denied: only config.write
        # sources become config.read.
        self.assertDecision(f"cd {self.home} && cp .ssh/id_ed25519 {self.repo}/x", "deny", "secrets")
        self.assertDecision(f"cd {self.control} && cp policy.jsonl {self.repo}/x.jsonl",
                            "deny", "control_plane.write")


if __name__ == "__main__":
    unittest.main()
