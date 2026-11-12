"""Tests for the central capability policy, its two adapters and its installation.

Every test uses a temporary HOME, state directory and repository, so neither the real policy log
nor the person's home is read or written. The policy CLI is exercised as a child process (the same
way both adapters call it) and the engine is also imported directly for the role matrix.
"""
import contextlib
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
HARNESS = ROOT / "harness"
POLICY_CLI = SRC / "policy.py"
PLUGIN = HARNESS / "opencode-policy.js"
NODE = shutil.which("node")

sys.path.insert(0, str(SRC))

import i18n  # noqa: E402
import installer  # noqa: E402
import policy  # noqa: E402
import yaml_min  # noqa: E402

EXPECTED_CAPABILITIES = {
    "repo.read", "repo.write", "repo.outside",
    "vcs.read", "vcs.write", "vcs.remote",
    "test.run", "net", "install", "admin",
    "config.read", "config.write", "tmp", "secrets", "session.external",
    "workspace.read", "workspace.write",
}

ENV_KEYS = (
    "HOME", "WS_STATE", "WS_SESSION_ROLE", "WS_SESSION_ROOT", "WS_SESSION_PARENT_ROLE",
    "WS_PARENT_ROLE", "WS_SESSION_ID", "WS_POLICY", "WS_POLICY_AUTO", "WS_AUTO", "WS_DIR",
    "WS_I18N", "WS_POLICY_CLI", "TMPDIR",
)


@contextlib.contextmanager
def patched_env(**values):
    saved = {name: os.environ.get(name) for name in values}
    for name, value in values.items():
        if value is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = str(value)
    try:
        yield
    finally:
        for name, value in saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


class PolicyTest(unittest.TestCase):
    def setUp(self):
        # Not under /tmp: /tmp is inside every box, so a temp repo/home there would mask the
        # outside-the-box cases. /var/tmp is still throwaway and is not part of the box.
        base = "/var/tmp" if os.access("/var/tmp", os.W_OK) else None
        self._tmp = tempfile.TemporaryDirectory(dir=base)
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name).resolve()
        self.home = self.root / "home"
        self.repo = self.root / "repo"
        self.state = self.root / "state"
        for folder in (self.home / ".config", self.repo):
            folder.mkdir(parents=True)
        self.outside = "/opt/ai-tmux-workspace-policy-test/outside.txt"
        self.saved = {name: os.environ.pop(name, None) for name in ENV_KEYS}
        os.environ.update(HOME=str(self.home), WS_STATE=str(self.state),
                          WS_SESSION_ROLE="repo", WS_SESSION_ROOT=str(self.repo))
        self.addCleanup(self._restore)

    def _restore(self):
        for name, value in self.saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value

    # --- helpers -------------------------------------------------------------------------------
    def env(self, **overrides):
        env = {name: value for name, value in os.environ.items() if name not in ENV_KEYS}
        env.update(HOME=str(self.home), WS_STATE=str(self.state),
                   WS_SESSION_ROLE="repo", WS_SESSION_ROOT=str(self.repo))
        for name, value in overrides.items():
            if value is None:
                env.pop(name, None)
            else:
                env[name] = str(value)
        return env

    def cli(self, event, **overrides):
        process = subprocess.run([sys.executable, str(POLICY_CLI), "check"],
                                 input=json.dumps(event), env=self.env(**overrides),
                                 capture_output=True, text=True, check=False)
        self.assertEqual(process.returncode, 0, process.stderr)
        return json.loads(process.stdout)

    def decide(self, event, **overrides):
        with patched_env(**overrides):
            return policy.check_event(event)

    def read(self, path, **extra):
        return {"tool_name": "Read", "tool_input": {"file_path": str(path)},
                "session_id": "sess-1", **extra}

    def write(self, path, **extra):
        return {"tool_name": "Write", "tool_input": {"file_path": str(path)},
                "session_id": "sess-1", **extra}

    def bash(self, command, **extra):
        return {"tool_name": "Bash", "tool_input": {"command": command},
                "session_id": "sess-1", **extra}

    def oread(self, path):
        return {"tool_name": "read", "tool_input": {"filePath": str(path)}, "session_id": "sess-1"}

    def owrite(self, path):
        return {"tool_name": "write", "tool_input": {"filePath": str(path)}, "session_id": "sess-1"}

    def obash(self, command):
        return {"tool_name": "bash", "tool_input": {"command": command}, "session_id": "sess-1"}

    def search(self, query, **extra):
        return {"tool_name": "ToolSearch", "tool_input": {"query": query},
                "session_id": "sess-1", **extra}

    def osearch(self, query):
        return {"tool_name": "toolsearch", "tool_input": {"query": query}, "session_id": "sess-1"}

    def fixture_policy(self, **overrides):
        document = yaml_min.loads((HARNESS / "policy.yaml").read_text(encoding="utf-8"))
        for role, entries in overrides.items():
            document["roles"][role].update(entries)
        path = self.root / "fixture-policy.yaml"
        path.write_text(yaml_min.dumps(document), encoding="utf-8")
        return path

    def hook(self, event, **overrides):
        payload = dict(event)
        payload.setdefault("hook_event_name", "PreToolUse")
        payload.setdefault("session_id", "sess-1")
        process = subprocess.run([sys.executable, str(SRC / "hook.py")],
                                 input=json.dumps(payload), env=self.env(**overrides),
                                 capture_output=True, text=True, check=False)
        self.assertEqual(process.returncode, 0, process.stderr)
        return json.loads(process.stdout) if process.stdout.strip() else None

    # --- policy file ---------------------------------------------------------------------------
    def test_policy_file_declares_default_deny_roles_and_exact_capabilities(self):
        document = yaml_min.loads((HARNESS / "policy.yaml").read_text(encoding="utf-8"))
        self.assertEqual(document["default"], "deny")
        self.assertEqual(set(document["roles"]), {"repo", "home", "subagent"})
        self.assertEqual(set(policy.CAPABILITIES), EXPECTED_CAPABILITIES)
        for role in ("repo", "home"):
            self.assertEqual(set(document["roles"][role]), EXPECTED_CAPABILITIES)
            for value in document["roles"][role].values():
                self.assertIn(value, policy.DECISIONS)
        self.assertEqual(document["roles"]["subagent"], {"inherits": "parent"})

    def test_policy_file_loads_through_the_engine(self):
        document = policy.load_policy(HARNESS / "policy.yaml")
        self.assertEqual(document["default"], "deny")

    # --- role matrix ---------------------------------------------------------------------------
    def test_repo_role_matrix(self):
        cases = [
            (self.read(self.repo / "a.py"), "repo.read", "allow"),
            (self.write(self.repo / "a.py"), "repo.write", "allow"),
            (self.write(self.outside), "repo.outside", "deny"),
            (self.read(self.home / ".config" / "app.conf"), "config.read", "allow"),
            (self.write(self.home / ".config" / "app.conf"), "config.write", "deny"),
            (self.write("/tmp/ai-tmux-workspace-policy-test.txt"), "tmp", "allow"),
            (self.read(self.repo / ".env"), "secrets", "deny"),
            (self.read(self.repo / ".env.example"), "repo.read", "allow"),
            (self.read(self.repo / "server.pem"), "secrets", "deny"),
            (self.bash("git status"), "vcs.read", "allow"),
            (self.bash("git commit -m done"), "vcs.write", "allow"),
            (self.bash("git push origin main"), "vcs.remote", "ask"),
            (self.bash("git config --global user.name"), "config.read", "allow"),
            (self.bash("git config --global user.name Someone"), "config.write", "deny"),
            (self.bash("python3 -m unittest discover -s tests"), "test.run", "allow"),
            (self.bash("curl https://example.com"), "net", "deny"),
            (self.bash("pip install requests"), "install", "deny"),
            (self.bash("sudo ls"), "admin", "deny"),
            ({"tool_name": "send_message", "tool_input": {}, "session_id": "s"},
             "session.external", "ask"),
        ]
        for event, capability, decision in cases:
            with self.subTest(event=event):
                result = self.decide(event)
                self.assertEqual(result["capability"], capability)
                self.assertEqual(result["decision"], decision)

    def test_home_role_matrix(self):
        with patched_env(WS_SESSION_ROLE="home", WS_SESSION_ROOT=str(self.home)):
            cases = [
                ({"tool_name": "Write",
                  "tool_input": {"file_path": str(self.home / "Codes" / "other" / "a.py")}},
                 "repo.write", "allow"),
                # the declared root (the home) wins over the config roots: the decision is the same
                # allow, now as the session's own repo instead of the machine config.
                (self.write(self.home / ".config" / "app.conf"), "repo.write", "allow"),
                (self.write("/opt/other/file.txt"), "repo.outside", "allow"),
                (self.bash("curl https://example.com"), "net", "ask"),
                (self.bash("pip install requests"), "install", "ask"),
                (self.bash("sudo ls"), "admin", "deny"),
                (self.read(self.home / ".env"), "secrets", "deny"),
                ({"tool_name": "send_message", "tool_input": {}}, "session.external", "ask"),
            ]
            for event, capability, decision in cases:
                with self.subTest(event=event):
                    result = policy.check_event(event)
                    self.assertEqual(result["capability"], capability)
                    self.assertEqual(result["decision"], decision)

    # --- workspace code protection (item 79) ---------------------------------------------------
    def test_workspace_code_is_protected_before_the_role_table(self):
        clone = self.root / "workspace-code"
        installed = self.home / ".local" / "share" / "ai-tmux-workspace" / "app"
        for folder in (clone / "src", installed / "src"):
            folder.mkdir(parents=True)
            (folder / "policy.py").write_text("x", encoding="utf-8")
        for code in (clone, installed):
            with self.subTest(code=code):
                for role, root in (("repo", self.repo), ("home", self.home)):
                    with patched_env(WS_DIR=str(code), WS_SESSION_ROLE=role,
                                     WS_SESSION_ROOT=str(root)):
                        read = policy.check_event(self.read(code / "src" / "policy.py"))
                        self.assertEqual((read["decision"], read["capability"]),
                                         ("allow", "workspace.read"))
                        for event in (self.write(code / "src" / "policy.py"),
                                      self.bash(f"echo x > {code}/src/policy.py")):
                            result = policy.check_event(event)
                            self.assertEqual(
                                (result["decision"], result["capability"]),
                                ("deny", "workspace.write"))
                with patched_env(WS_DIR=str(code), WS_SESSION_ROLE="repo",
                                 WS_SESSION_ROOT=str(code)):
                    for event in (self.write(code / "src" / "policy.py"),
                                  self.bash(f"echo x > {code}/src/policy.py")):
                        result = policy.check_event(event)
                        self.assertEqual(
                            (result["decision"], result["capability"]),
                            ("allow", "workspace.write"))
                        self.assertEqual(result["reason"], "policy.reason.workspace_self")

    def test_workspace_protection_resolves_symlinks_and_relative_paths(self):
        code = self.root / "workspace-code"
        code.mkdir()
        target = code / "policy.py"
        target.write_text("x", encoding="utf-8")
        link = self.repo / "link.py"
        link.symlink_to(target)
        with patched_env(WS_DIR=str(code)):
            for value in (link, os.path.relpath(target, self.repo)):
                with self.subTest(path=value):
                    result = policy.check_event(self.write(value))
                    self.assertEqual((result["decision"], result["capability"]),
                                     ("deny", "workspace.write"))

    def test_workspace_root_falls_back_to_the_policy_file_location(self):
        with patched_env(WS_DIR=None):
            self.assertEqual(policy._workspace_root(), str(ROOT))
            write = policy.check_event(self.write(ROOT / "src" / "policy.py"))
            self.assertEqual((write["decision"], write["capability"]),
                             ("deny", "workspace.write"))
            read = policy.check_event(self.read(ROOT / "src" / "policy.py"))
            self.assertEqual((read["decision"], read["capability"]),
                             ("allow", "workspace.read"))

    # --- fail closed ---------------------------------------------------------------------------
    def test_unknown_and_ambiguous_actions_fail_closed(self):
        cases = [
            {"tool_name": "mcp__server__tool", "tool_input": {}},
            {"tool_name": "UnknownTool", "tool_input": {}},
            self.bash("python3 -c 'import os'"),
            self.bash("sh -c 'rm -rf /'"),
            self.bash("git"),
            self.bash("rm"),
            self.bash("cat $(echo /etc/passwd)"),
            self.bash("find . -delete"),
            self.bash("diff --from-file=.env a.txt"),
            self.bash("diff --to-file=.env a.txt"),
            self.bash("wc --files0-from=.env"),
            self.bash("grep --exclude-from=.env x a.txt"),
            self.bash("rg --ignore-file=.env x ."),
            self.bash("tree --fromfile=.env"),
            {"tool_name": "Write", "tool_input": {}},
        ]
        for event in cases:
            with self.subTest(event=event):
                result = self.decide(event)
                self.assertEqual(result["decision"], "deny")
                self.assertIsNone(result["capability"])

    # --- regression: shell expansion, globs and ambiguous paths --------------------------------
    def test_shell_expansion_and_globs_are_not_paths(self):
        commands = [
            "rm -rf $HOME",
            "rm -rf ${HOME}/x",
            "echo x > $HOME/file",
            "tee $HOME/x",
            "cat *",
            "cat .env*",
            "rm *",
            "sed -i 's/x/y/' *.py",
            "awk '{print}' *.py",
            "grep x *.py",
            "cat ~zzz_nonexistent_user_zzz/file",
        ]
        for command in commands:
            with self.subTest(command=command):
                result = self.decide(self.bash(command))
                self.assertEqual(result["decision"], "deny")
                self.assertIsNone(result["capability"])

    def test_scripts_and_patterns_with_expansion_are_skipped(self):
        commands = [
            "grep '$HOME' file.txt",
            "grep '*.py' file.txt",
            "grep -e '$HOME' file.txt",
            "find . -name '*.py'",
        ]
        for command in commands:
            with self.subTest(command=command):
                result = self.decide(self.bash(command))
                self.assertEqual(result["decision"], "allow")
                self.assertEqual(result["capability"], "repo.read")

    def test_sed_awk_and_pagers_are_denied(self):
        commands = [
            "sed -e 's/x/y/' a.txt",
            "sed -e 's/x/y/w /etc/foo' a.txt",
            "awk 'BEGIN{print \"x\" > \"/etc/foo\"}'",
            "awk '{print $1}' file.txt",
            "gawk '{print}' file.txt",
            "mawk '{print}' file.txt",
            "less file.txt",
            "more file.txt",
        ]
        for command in commands:
            with self.subTest(command=command):
                result = self.decide(self.bash(command))
                self.assertEqual(result["decision"], "deny")
                self.assertIsNone(result["capability"])

    def test_tilde_expands_instead_of_being_rejected(self):
        result = self.decide(self.bash("cat ~"))
        self.assertEqual((result["decision"], result["capability"]), ("deny", "repo.outside"))
        with patched_env(WS_SESSION_ROLE="home", WS_SESSION_ROOT=str(self.home)):
            result = policy.check_event(self.bash("cat ~"))
            self.assertEqual((result["decision"], result["capability"]), ("allow", "repo.read"))

    def test_grep_checks_every_file_after_the_pattern(self):
        cases = [
            ("grep x .env a.txt", "secrets", "deny"),
            ("grep x a.txt .env", "secrets", "deny"),
            ("grep -f .env a.txt", "secrets", "deny"),
            ("grep -e x .env", "secrets", "deny"),
            ("grep x a.txt b.txt", "repo.read", "allow"),
            ("grep '*.py' a.txt", "repo.read", "allow"),
        ]
        for command, capability, decision in cases:
            with self.subTest(command=command):
                result = self.decide(self.bash(command))
                self.assertEqual((result["decision"], result["capability"]), (decision, capability))

    def test_redirect_operator_ampersand_is_a_write(self):
        cases = [
            ("echo x >& /etc/foo", "repo.outside", "deny"),
            ("echo x >& /tmp/foo", "tmp", "allow"),
            ("echo x &> /etc/foo", "repo.outside", "deny"),
            ("echo x >&2", "repo.read", "allow"),
            ("echo x 2>&1", "repo.read", "allow"),
            ("echo x >&-", "repo.read", "allow"),
        ]
        for command, capability, decision in cases:
            with self.subTest(command=command):
                result = self.decide(self.bash(command))
                self.assertEqual((result["decision"], result["capability"]), (decision, capability))

    def test_git_secret_paths_are_secrets(self):
        commands = [
            "git show HEAD:.env",
            "git diff .env",
            "git add .env",
            "git checkout .env",
            "git diff -- .env",
            "git show HEAD:.ssh/id_rsa",
            "git show HEAD:.aws/credentials",
        ]
        for command in commands:
            with self.subTest(command=command):
                result = self.decide(self.bash(command))
                self.assertEqual((result["decision"], result["capability"]), ("deny", "secrets"))
        for command in ("git commit -m 'fix .env'", "git commit -am 'fix .env'",
                        "git show HEAD:src/app.py", "git log --grep '.env'"):
            with self.subTest(command=command):
                self.assertEqual(self.decide(self.bash(command))["decision"], "allow")
        self.assertEqual(self.decide(self.bash("git add '*.env'"))["decision"], "deny")

    def test_known_credential_stores_are_secrets(self):
        paths = (
            "~/.config/gh/hosts.yml",
            "~/.config/gcloud/application_default_credentials.json",
            "~/.aws/credentials",
            "~/.docker/config.json",
            "~/.kube/config",
            "~/.config/hub",
            "~/.npm/_cacache/x",
            "~/.git-credentials",
            "~/.netrc",
        )
        for path in paths:
            with self.subTest(path=path):
                result = self.decide(self.bash(f"cat {path}"))
                self.assertEqual((result["decision"], result["capability"]), ("deny", "secrets"))
        self.assertEqual(self.decide(self.bash("cat ~/.config/other.conf"))["capability"], "config.read")
        self.assertEqual(self.decide(self.bash("cat ~/.config/gh/hosts.yml.example"))["decision"],
                         "allow")

    def test_tmpdir_outside_tmp_does_not_enlarge_the_box(self):
        outside = "/var/tmp/ai-tmux-workspace-elsewhere/x"
        with patched_env(TMPDIR="/var/tmp/elsewhere"):
            result = self.decide(self.write(outside))
            self.assertEqual((result["decision"], result["capability"]), ("deny", "repo.outside"))
            self.assertEqual(self.decide(self.write("/tmp/policy-regression.txt"))["capability"], "tmp")
            fixture = self.fixture_policy(repo={"repo.outside": "ask"})
            with patched_env(WS_POLICY=str(fixture), WS_POLICY_AUTO="1",
                             TMPDIR="/var/tmp/elsewhere"):
                self.assertEqual(self.decide(self.write(outside))["decision"], "ask")

    def test_audit_target_for_path_findings_is_the_resolved_path(self):
        self.cli(self.bash("cat /etc/passwd"))
        self.cli(self.bash("grep SUPERSECRET-GREP file.txt"))
        self.cli(self.bash("sort -k SUPERSECRET-KEY file.txt"))
        self.cli(self.bash("git commit -m SUPERSECRET-MESSAGE"))
        path = self.state / "policy.jsonl"
        entries = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]
        self.assertEqual(len(entries), 4)
        self.assertEqual(entries[0]["target"], "/etc/passwd")
        self.assertTrue(entries[1]["target"].endswith("/file.txt"))
        self.assertEqual(entries[2]["target"], "sort")
        self.assertEqual(entries[3]["target"], "git commit")
        text = path.read_text(encoding="utf-8")
        for secret in ("SUPERSECRET-GREP", "SUPERSECRET-KEY", "SUPERSECRET-MESSAGE"):
            self.assertNotIn(secret, text)

    def test_read_program_write_options_are_writes(self):
        for command in ("sort -o /etc/foo file", "sort --output=/etc/foo file",
                        "tree -o /etc/foo ."):
            with self.subTest(command=command):
                result = self.decide(self.bash(command))
                self.assertEqual((result["decision"], result["capability"]), ("deny", "repo.outside"))
        result = self.decide(self.bash("sort -o ~/.config/x file"))
        self.assertEqual((result["decision"], result["capability"]), ("deny", "config.write"))

    def test_target_directory_options_are_writes(self):
        for command in ("cp a.txt --target-directory=/etc", "cp -t/etc a.txt",
                        "cp -st/etc a.txt", "mv --target-directory=/etc a.txt",
                        "ln -t/etc a.txt", "install -t /etc a.txt"):
            with self.subTest(command=command):
                result = self.decide(self.bash(command))
                self.assertEqual((result["decision"], result["capability"]), ("deny", "repo.outside"))
        self.assertEqual(self.decide(self.bash("cp -t/tmp a.txt"))["decision"], "allow")
        result = self.decide(self.bash("cp -t ~/.config a.txt"))
        self.assertEqual((result["decision"], result["capability"]), ("deny", "config.write"))

    def test_find_write_and_symlink_options_are_denied(self):
        for command in ("find . -fprint /etc/foo", "find . -fprint0 /etc/foo",
                        "find . -fprintf /etc/foo %p", "find . -fls /etc/foo",
                        "find -L .", "find -H .", "find . -follow"):
            with self.subTest(command=command):
                result = self.decide(self.bash(command))
                self.assertEqual(result["decision"], "deny")
                self.assertIsNone(result["capability"])
        self.assertEqual(self.decide(self.bash("find . -name '*.py'"))["capability"], "repo.read")

    def test_git_combined_short_message_does_not_hide_a_path(self):
        for command in ("git commit -mmsg .env", "git commit -am msg .env",
                        "git commit -Ffile .env", "git commit -m msg .env"):
            with self.subTest(command=command):
                result = self.decide(self.bash(command))
                self.assertEqual((result["decision"], result["capability"]), ("deny", "secrets"))
        for command in ("git commit -mmsg", "git commit -am msg", "git commit -Ffile"):
            with self.subTest(command=command):
                self.assertEqual(self.decide(self.bash(command))["decision"], "allow")

    def test_recursive_grep_and_hidden_rg_are_denied(self):
        for command in ("grep -r x .", "grep -R x .", "grep --recursive x .",
                        "grep --dereference-recursive x .", "grep -rn x .",
                        "grep -d recurse x .", "grep -drecurse x .",
                        "rg --hidden x .", "rg -u x .", "rg -uu x .",
                        "rg --unrestricted x .", "rg --no-ignore x .",
                        "rg --no-ignore-vcs x .", "rg -L x .", "rg --follow x ."):
            with self.subTest(command=command):
                result = self.decide(self.bash(command))
                self.assertEqual(result["decision"], "deny")
                self.assertIsNone(result["capability"])
        for command in ("rg x .", "grep -er x .", "grep x .", "rg -e-u x ."):
            with self.subTest(command=command):
                self.assertEqual(self.decide(self.bash(command))["decision"], "allow")

    def test_dangerous_read_program_options_are_denied(self):
        for command in ("sort --compress-program=sh file", "sort --files0-from=.env file",
                        "sort --random-source=/etc/passwd file", "sort -T /tmp file",
                        "sort --temporary-directory=/tmp file", "sort -nT/tmp file",
                        "file -C", "file --compile file", "file -f .env",
                        "file --files-from=.env", "file -m /etc/magic file"):
            with self.subTest(command=command):
                result = self.decide(self.bash(command))
                self.assertEqual(result["decision"], "deny")
                self.assertIsNone(result["capability"])
        for command in ("sort file.txt", "sort -o /tmp/out.txt file.txt", "file file.txt"):
            with self.subTest(command=command):
                self.assertEqual(self.decide(self.bash(command))["decision"], "allow")

    def test_cli_without_role_fails_closed(self):
        result = self.cli(self.read(self.repo / "a.py"), WS_SESSION_ROLE=None)
        self.assertEqual(result, {"decision": "deny", "capability": None,
                                  "reason": "policy.reason.no_role"})

    def test_unknown_role_fails_closed(self):
        result = self.decide(self.read(self.repo / "a.py"), WS_SESSION_ROLE="admin")
        self.assertEqual(result["decision"], "deny")
        self.assertEqual(result["reason"], "policy.reason.unknown_role")

    def test_missing_or_invalid_policy_fails_closed(self):
        missing = self.root / "missing.yaml"
        result = self.decide(self.read(self.repo / "a.py"), WS_POLICY=str(missing))
        self.assertEqual(result["decision"], "deny")
        self.assertEqual(result["reason"], "policy.reason.invalid")

        invalid = self.root / "invalid.yaml"
        invalid.write_text("version: 1\ndefault: allow\nroles: {}\n", encoding="utf-8")
        result = self.decide(self.read(self.repo / "a.py"), WS_POLICY=str(invalid))
        self.assertEqual(result["decision"], "deny")
        self.assertEqual(result["reason"], "policy.reason.invalid")

    def test_invalid_cli_input_returns_a_decision(self):
        process = subprocess.run([sys.executable, str(POLICY_CLI), "check"],
                                 input="not json", env=self.env(),
                                 capture_output=True, text=True, check=False)
        self.assertEqual(process.returncode, 0)
        self.assertEqual(json.loads(process.stdout)["decision"], "deny")

    # --- auto ----------------------------------------------------------------------------------
    def test_auto_only_upgrades_ask_inside_the_box_and_never_deny(self):
        fixture = self.fixture_policy(repo={
            "repo.write": "ask",
            "repo.outside": "ask",
            "config.write": "ask",
            "tmp": "ask",
            "vcs.write": "ask",
        })
        inside = self.write(self.repo / "a.py")
        with patched_env(WS_POLICY=str(fixture)):
            self.assertEqual(self.decide(inside)["decision"], "ask")
            with patched_env(WS_POLICY_AUTO="1"):
                self.assertEqual(self.decide(inside)["decision"], "allow")
                self.assertEqual(self.decide(inside)["reason"], "policy.reason.auto")
                self.assertEqual(self.decide(self.write(self.outside))["decision"], "ask")
                self.assertEqual(self.decide(self.write("/tmp/outside.txt"))["decision"], "allow")
                self.assertEqual(self.decide(self.write(self.home / ".config" / "x"))["decision"], "ask")
                self.assertEqual(self.decide(self.bash("git commit -m x"))["decision"], "ask")
                self.assertEqual(self.decide(self.bash("sudo ls"))["decision"], "deny")
                self.assertEqual(self.decide(self.read(self.repo / ".env"))["decision"], "deny")

    def test_auto_from_the_event_permission_mode(self):
        fixture = self.fixture_policy(repo={"repo.write": "ask"})
        event = self.write(self.repo / "a.py")
        event["permission_mode"] = "auto"
        with patched_env(WS_POLICY=str(fixture)):
            self.assertEqual(self.decide(event)["decision"], "allow")

    def test_auto_without_a_declared_box_does_not_upgrade(self):
        fixture = self.fixture_policy(repo={"repo.outside": "ask"})
        event = self.write(self.repo / "a.py")
        with patched_env(WS_POLICY=str(fixture), WS_SESSION_ROOT=None, WS_POLICY_AUTO="1"):
            self.assertEqual(self.decide(event)["decision"], "ask")

    # --- session.external and subagents --------------------------------------------------------
    def test_session_external_is_ask_for_every_role(self):
        external = {"tool_name": "session_message", "tool_input": {}, "session_id": "s"}
        self.assertEqual(self.decide(external)["decision"], "ask")
        with patched_env(WS_SESSION_ROLE="home", WS_SESSION_ROOT=str(self.home)):
            self.assertEqual(policy.check_event(external)["decision"], "ask")
        self.assertEqual(self.decide(dict(external, agent_id="a1"))["decision"], "ask")

    def test_subagent_inherits_the_parent_without_amplifying(self):
        parent_repo = {
            "net": self.bash("curl https://example.com", agent_id="a1"),
            "vcs.remote": self.bash("git push", agent_id="a1"),
            "repo.read": self.read(self.repo / "a.py", agent_id="a1"),
        }
        self.assertEqual(self.decide(parent_repo["net"])["decision"], "deny")
        self.assertEqual(self.decide(parent_repo["vcs.remote"])["decision"], "ask")
        self.assertEqual(self.decide(parent_repo["repo.read"])["decision"], "allow")

        with patched_env(WS_SESSION_ROLE="home", WS_SESSION_ROOT=str(self.home)):
            event = self.bash("curl https://example.com", agent_id="a1")
            self.assertEqual(policy.check_event(event)["decision"], "ask")

    def test_explicit_subagent_role_uses_the_parent_role(self):
        event = self.bash("git push")
        with patched_env(WS_SESSION_ROLE="subagent", WS_SESSION_PARENT_ROLE="repo"):
            self.assertEqual(policy.check_event(event)["decision"], "ask")
        with patched_env(WS_SESSION_ROLE="subagent", WS_SESSION_PARENT_ROLE=None):
            result = policy.check_event(event)
            self.assertEqual(result["decision"], "deny")
            self.assertEqual(result["reason"], "policy.reason.no_parent")

    def test_subagent_entries_can_only_tighten_the_parent(self):
        fixture = self.fixture_policy(subagent={
            "inherits": "parent",
            "net": "allow",           # parent repo denies net: ignored
            "repo.write": "deny",     # parent repo allows: accepted (stricter)
        })
        with patched_env(WS_POLICY=str(fixture)):
            self.assertEqual(self.decide(self.bash("curl https://x", agent_id="a1"))["decision"], "deny")
            self.assertEqual(self.decide(self.write(self.repo / "a.py", agent_id="a1"))["decision"],
                             "deny")
            self.assertEqual(self.decide(self.write(self.repo / "a.py"))["decision"], "allow")

    # --- tool search and the subagent's own messages (item 115) --------------------------------
    def test_tool_search_select_of_inert_names_is_inert(self):
        queries = (
            "select:SendMessage,ExitPlanMode",
            "select:sendmessage,exitplanmode",
            "select: SendMessage, ExitPlanMode",
            "select:SubagentHandback",
            "select:subagenthandback",
            "select:Task",
            "select:TodoWrite,Question,Skill",
        )
        for query in queries:
            with self.subTest(query=query):
                result = self.decide(self.search(query))
                self.assertEqual((result["decision"], result["capability"]), ("allow", None))
                self.assertEqual(result["reason"], "policy.reason.inert")

        # Loading a tool is not the same as calling it: the loaded tool is classified one by one.
        self.assertEqual(self.decide(self.bash("curl https://example.com"))["decision"], "deny")
        self.assertEqual(self.decide(self.bash("git push"))["decision"], "ask")

    def test_tool_search_other_queries_and_names_outside_the_list_fail_closed(self):
        queries = (
            "sendmessage",
            "select:",
            "select: ",
            "select:SendMessage,",
            "select:,SendMessage",
            "select:Bash",
            "select:Write",
            "select:WebFetch",
            "select:SendMessage,UnknownTool",
            "match:read",
            "select",
        )
        for query in queries:
            with self.subTest(query=query):
                result = self.decide(self.search(query))
                self.assertEqual((result["decision"], result["capability"]), ("deny", None))
                self.assertEqual(result["reason"], "policy.reason.unknown")

    def test_tool_search_missing_or_non_dict_input_fails_closed(self):
        events = (
            {"tool_name": "ToolSearch", "tool_input": None, "session_id": "s"},
            {"tool_name": "ToolSearch", "tool_input": {}, "session_id": "s"},
            {"tool_name": "ToolSearch", "tool_input": ["select:Task"], "session_id": "s"},
            {"tool_name": "ToolSearch", "tool_input": {"query": None}, "session_id": "s"},
            {"tool_name": "ToolSearch", "tool_input": {"query": 3}, "session_id": "s"},
            {"tool_name": "ToolSearch", "session_id": "s"},
        )
        for event in events:
            with self.subTest(event=event):
                result = self.decide(event)
                self.assertEqual((result["decision"], result["capability"]), ("deny", None))
                self.assertEqual(result["reason"], "policy.reason.unknown")

    def test_send_message_is_inert_inside_the_session_tree(self):
        for tool in ("SendMessage", "sendmessage"):
            with self.subTest(tool=tool):
                for extra in ({}, {"agent_id": "a1"}):
                    result = self.decide({"tool_name": tool, "tool_input": {},
                                          "session_id": "s", **extra})
                    self.assertEqual((result["decision"], result["capability"]), ("allow", None))
                    self.assertEqual(result["reason"], "policy.reason.inert")
        with patched_env(WS_SESSION_ROLE="subagent", WS_SESSION_PARENT_ROLE="repo"):
            result = policy.check_event({"tool_name": "SendMessage", "tool_input": {},
                                         "session_id": "s"})
            self.assertEqual((result["decision"], result["capability"]), ("allow", None))

    def test_explicit_external_session_on_send_message_still_asks(self):
        for tool_input in ({"external_session": True}, {"origin": "external_session"}):
            with self.subTest(tool_input=tool_input):
                result = self.decide({"tool_name": "SendMessage", "tool_input": tool_input,
                                      "session_id": "s"})
                self.assertEqual((result["decision"], result["capability"]),
                                 ("ask", "session.external"))

    def test_other_session_message_tools_remain_external(self):
        for tool in ("send_message", "session_message", "session_request", "cross_session",
                     "conference"):
            with self.subTest(tool=tool):
                result = self.decide({"tool_name": tool, "tool_input": {}, "session_id": "s"})
                self.assertEqual((result["decision"], result["capability"]),
                                 ("ask", "session.external"))

    def test_subagent_handback_is_inert(self):
        for tool in ("SubagentHandback", "subagenthandback"):
            with self.subTest(tool=tool):
                result = self.decide({"tool_name": tool, "tool_input": {}, "session_id": "s"})
                self.assertEqual((result["decision"], result["capability"]), ("allow", None))
                self.assertEqual(result["reason"], "policy.reason.inert")

    def test_tool_search_does_not_resolve_a_path_or_use_the_root(self):
        event = self.search("select:SendMessage")
        with patched_env(WS_SESSION_ROOT=None):
            result = self.decide(event)
            self.assertEqual((result["decision"], result["capability"]), ("allow", None))
            self.assertEqual(result["reason"], "policy.reason.inert")
        self.cli(event)
        entries = [json.loads(line) for line in
                   (self.state / "policy.jsonl").read_text(encoding="utf-8").splitlines() if line]
        self.assertTrue(entries)
        for entry in entries:
            self.assertEqual(entry["target"], "toolsearch")

    def test_cli_covers_the_item_115_acceptance_list(self):
        cases = (
            ({"tool_name": "ToolSearch",
              "tool_input": {"query": "select:SendMessage,ExitPlanMode"}}, "allow", None),
            ({"tool_name": "ToolSearch", "tool_input": {"query": "select:Bash"}},
             "deny", None),
            ({"tool_name": "ToolSearch", "tool_input": {"query": "sendmessage"}},
             "deny", None),
            ({"tool_name": "SendMessage", "tool_input": {}}, "allow", None),
            ({"tool_name": "SendMessage", "tool_input": {"external_session": True}},
             "ask", "session.external"),
            ({"tool_name": "SubagentHandback", "tool_input": {}}, "allow", None),
        )
        for event, decision, capability in cases:
            with self.subTest(event=event):
                result = self.cli(event)
                self.assertEqual((result["decision"], result["capability"]),
                                 (decision, capability))

    # --- git -C, cd and godot --headless inside the box (item 116) -----------------------------
    def test_git_dash_c_inside_the_box_classifies_the_subcommand(self):
        worktree = self.repo / ".claude" / "worktrees" / "x"
        cases = (
            ("git -C .claude/worktrees/x status", "vcs.read"),
            ("git -C .claude/worktrees/x diff", "vcs.read"),
            ("git -C .claude/worktrees/x log", "vcs.read"),
            ("git -C .claude/worktrees/x commit -m x", "vcs.write"),
            (f"git -C {worktree} status", "vcs.read"),
            ("git -C /tmp status", "vcs.read"),
            # the second -C is resolved relative to the first, as git does
            ("git -C sub -C .. status", "vcs.read"),
        )
        for command, capability in cases:
            with self.subTest(command=command):
                result = self.decide(self.bash(command))
                self.assertEqual((result["decision"], result["capability"]),
                                 ("allow", capability))

    def test_git_dash_c_outside_the_box_or_ambiguous_stays_denied(self):
        commands = (
            "git -C /home/qualquer status",
            "git -C ../outside status",
            "git -C .claude/worktrees/x -C /home/qualquer status",
            "git -C",
            "git -C. status",
            "git --git-dir=.git status",
            "git --work-tree=. status",
            "git -c core.pager=cat status",
        )
        for command in commands:
            with self.subTest(command=command):
                result = self.decide(self.bash(command))
                self.assertEqual((result["decision"], result["capability"]), ("deny", None))
        result = self.decide(self.bash("git -C .env status"))
        self.assertEqual((result["decision"], result["capability"]), ("deny", "secrets"))

    def test_cd_reanchors_the_following_segments_inside_the_box(self):
        worktree = self.repo / ".claude" / "worktrees" / "x"
        cases = (
            ("cd .claude/worktrees/x && git status", "vcs.read"),
            ("cd .claude/worktrees/x; git diff", "vcs.read"),
            ("cd -L .claude/worktrees/x && git log", "vcs.read"),
            ("cd -P .claude/worktrees/x && git log", "vcs.read"),
            ("cd .claude/worktrees/x && cat README.md", "repo.read"),
            (f"cd {worktree} && python3 -I -m unittest discover -s testes/validador", "test.run"),
            ("cd /tmp && git status", "vcs.read"),
            # each cd reanchors from the previous one, still inside the box
            ("cd .claude/worktrees/x && cd .. && git status", "vcs.read"),
        )
        for command, capability in cases:
            with self.subTest(command=command):
                result = self.decide(self.bash(command))
                self.assertEqual((result["decision"], result["capability"]),
                                 ("allow", capability))

    def test_cd_outside_the_box_or_ambiguous_stays_denied(self):
        commands = (
            "cd && git status",
            "cd - && git status",
            "cd -- .claude/worktrees/x && git status",
            "cd .. && git status",
            "cd ../.. && git status",
            "cd /home/qualquer && git status",
            "cd ~ && git status",
            "cd $HOME && git status",
            "cd *.d && git status",
            "cd -a .claude/worktrees/x && git status",
            "cd .claude/worktrees/x extra && git status",
        )
        for command in commands:
            with self.subTest(command=command):
                result = self.decide(self.bash(command))
                self.assertEqual((result["decision"], result["capability"]), ("deny", None))

    def test_cd_and_git_dash_c_do_not_enter_secret_directories(self):
        with patched_env(WS_SESSION_ROLE="home", WS_SESSION_ROOT=str(self.home)):
            result = policy.check_event(self.bash("cd .ssh && git status"))
            self.assertEqual((result["decision"], result["capability"]), ("deny", None))
            result = policy.check_event(self.bash("git -C .ssh status"))
            self.assertEqual((result["decision"], result["capability"]), ("deny", "secrets"))

    def test_cd_does_not_turn_tmp_paths_into_repo_paths(self):
        # The anchor only changes where relative paths resolve; the capability still comes from
        # the session box, so a tightened `tmp` is not reached as `repo.write`.
        fixture = self.fixture_policy(repo={"tmp": "deny"})
        with patched_env(WS_POLICY=str(fixture)):
            result = self.decide(self.bash("cd /tmp && touch x"))
            self.assertEqual((result["decision"], result["capability"]), ("deny", "tmp"))
            result = self.decide(self.bash("touch x"))
            self.assertEqual((result["decision"], result["capability"]), ("allow", "repo.write"))

    def test_cd_reanchoring_does_not_persist_between_calls(self):
        self.assertEqual(self.decide(self.bash("cd .claude/worktrees/x && git status"))["decision"],
                         "allow")
        # with the anchor leaked from the previous call, `cd ..` would still be inside the box
        self.assertEqual(self.decide(self.bash("cd .. && git status"))["decision"], "deny")

    def test_python_flags_before_dash_m_keep_the_module_classification(self):
        commands = (
            "python3 -I -m unittest discover -s tests",
            "python3 -B -m pytest",
            "python3 -u -E -s -S -O -OO -m unittest",
            "python3 -X dev -m unittest",
            "python3 -Xdev -m pytest",
            "python3 -W ignore::DeprecationWarning -m unittest",
            "python3 -Wignore -m tox",
            "python3 -I -m nox",
            "python3 -I -m pip list",
        )
        for command in commands:
            with self.subTest(command=command):
                result = self.decide(self.bash(command))
                self.assertEqual((result["decision"], result["capability"]),
                                 ("allow", "test.run"))
        result = self.decide(self.bash("python3 -I -m pip install requests"))
        self.assertEqual((result["decision"], result["capability"]), ("deny", "install"))
        for command in ("python3 -c 'print(1)'", "python3 -I -c 'print(1)'",
                        "python3 -I -m unknown_module", "python3 -I -",
                        "python3 -I tests/run.py", "python3 -v -m unittest",
                        "python3 --help"):
            with self.subTest(command=command):
                result = self.decide(self.bash(command))
                self.assertEqual((result["decision"], result["capability"]), ("deny", None))

    def test_godot_requires_headless_and_paths_inside_the_box(self):
        commands = (
            "godot --headless --quit",
            "godot4 --headless --quit",
            "godot --headless --path . --quit",
            "godot --headless --path=. --quit",
            "godot --headless --path /tmp/project --quit",
            "godot --headless --main-pack game.pck",
            "godot --headless --main-pack=game.pck",
            "godot --headless --script teste.gd",
            "godot --headless --script=teste.gd",
            "godot --headless -s teste.gd",
            "godot --headless project.godot",
            "cd .claude/worktrees/y && godot --headless --quit",
        )
        for command in commands:
            with self.subTest(command=command):
                result = self.decide(self.bash(command))
                self.assertEqual((result["decision"], result["capability"]),
                                 ("allow", "test.run"))
        for command in ("godot --quit", "godot4 --quit",
                        "godot --headless --export-release",
                        "godot --headless --export-debug",
                        "godot --headless --path /home/qualquer",
                        "godot --headless --path=/home/qualquer",
                        "godot --headless --main-pack /etc/game.pck",
                        "godot --headless --script /etc/teste.gd",
                        "godot --headless -s /etc/teste.gd",
                        "godot --headless /etc/project.godot",
                        "godot --headless --path"):
            with self.subTest(command=command):
                result = self.decide(self.bash(command))
                self.assertEqual((result["decision"], result["capability"]), ("deny", None))
        result = self.decide(self.bash("godot --headless --script .env"))
        self.assertEqual((result["decision"], result["capability"]), ("deny", "secrets"))

    def test_cli_covers_the_item_116_acceptance_battery(self):
        worktree = self.repo / ".claude" / "worktrees"
        cases = (
            (f"cd {worktree / 'x'} && python3 -I -m unittest discover -s testes/validador",
             "allow", "test.run"),
            (f"cd {worktree / 'y'} && godot --headless --quit", "allow", "test.run"),
            ("cd .claude/worktrees/x && git status", "allow", "vcs.read"),
            ("git -C .claude/worktrees/x status", "allow", "vcs.read"),
            ("git -C .claude/worktrees/x diff", "allow", "vcs.read"),
            ("git -C .claude/worktrees/x log", "allow", "vcs.read"),
            ("git -C /home/qualquer status", "deny", None),
            ("cd .. && git status", "deny", None),
            ("python3 -c 'print(1)'", "deny", None),
            ("godot --quit", "deny", None),
            ("godot --headless --export-release", "deny", None),
        )
        for command, decision, capability in cases:
            with self.subTest(command=command):
                result = self.cli(self.bash(command))
                self.assertEqual((result["decision"], result["capability"]),
                                 (decision, capability))

    # --- audit log -----------------------------------------------------------------------------
    def test_log_is_0600_and_never_contains_a_secret(self):
        secret = "SUPERSECRET-token-value"
        self.cli(self.bash(f'curl -H "Authorization: Bearer {secret}" https://example.com'))
        self.cli(self.bash(f'git commit -m "{secret}"'))
        self.cli(self.bash(f"sort -k {secret} file.txt"))
        self.cli(self.bash(f"cut -d {secret} -f1 file.txt"))
        self.cli({"tool_name": "Write",
                  "tool_input": {"file_path": str(self.repo / "note.txt"), "content": secret}})
        path = self.state / "policy.jsonl"
        self.assertTrue(path.is_file())
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        text = path.read_text(encoding="utf-8")
        self.assertNotIn(secret, text)
        lines = [json.loads(line) for line in text.splitlines() if line.strip()]
        self.assertEqual(len(lines), 5)
        for entry in lines:
            for key in ("role", "session", "capability", "target", "decision", "reason"):
                self.assertIn(key, entry)
            self.assertEqual(entry["role"], "repo")

    def test_log_mode_is_repaired_when_the_file_already_exists(self):
        self.state.mkdir(parents=True, exist_ok=True)
        path = self.state / "policy.jsonl"
        path.write_text("", encoding="utf-8")
        os.chmod(path, 0o644)
        self.cli(self.read(self.repo / "a.py"))
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    # --- Claude hook ---------------------------------------------------------------------------
    def test_hook_returns_the_engine_decision(self):
        ask = self.hook(self.bash("git push"))
        self.assertEqual(ask["hookSpecificOutput"]["permissionDecision"], "ask")
        self.assertEqual(ask["hookSpecificOutput"]["permissionDecisionReason"],
                         i18n.t("policy.reason.ask", capability="vcs.remote"))

        deny = self.hook(self.bash("sudo ls"))
        self.assertEqual(deny["hookSpecificOutput"]["permissionDecision"], "deny")
        self.assertEqual(deny["hookSpecificOutput"]["permissionDecisionReason"],
                         i18n.t("policy.reason.deny", capability="admin"))

        self.assertIsNone(self.hook(self.bash("git status")))
        self.assertIsNone(self.hook({"tool_name": "TodoWrite", "tool_input": {}}))
        self.assertEqual(self.hook({"tool_name": "UnknownTool", "tool_input": {}})
                         ["hookSpecificOutput"]["permissionDecision"], "deny")

    def test_hook_without_a_declared_role_keeps_current_sessions_working(self):
        self.assertIsNone(self.hook(self.bash("sudo ls"), WS_SESSION_ROLE=None))
        self.assertIsNone(self.hook({"tool_name": "UnknownTool", "tool_input": {}},
                                    WS_SESSION_ROLE=None))

    def test_hook_emits_allow_only_for_an_auto_upgrade(self):
        self.assertIsNone(self.hook(self.bash("git status")))       # plain allow: no override
        fixture = self.fixture_policy(repo={"repo.write": "ask"})
        event = self.write(self.repo / "a.py")
        self.assertEqual(self.hook(event, WS_POLICY=str(fixture))
                         ["hookSpecificOutput"]["permissionDecision"], "ask")
        result = self.hook(event, WS_POLICY=str(fixture), WS_POLICY_AUTO="1")
        self.assertEqual(result["hookSpecificOutput"]["permissionDecision"], "allow")
        self.assertEqual(result["hookSpecificOutput"]["permissionDecisionReason"],
                         i18n.t("policy.reason.auto", capability="repo.write"))

    def test_hook_never_breaks_on_a_broken_policy(self):
        output = self.hook(self.bash("git status"), WS_POLICY=str(self.root))
        self.assertEqual(output["hookSpecificOutput"]["permissionDecision"], "deny")

    # --- adapters: same engine, same decisions -------------------------------------------------
    def _battery(self):
        return [
            (self.read(self.repo / "a.py"), self.oread(self.repo / "a.py")),
            (self.write(self.repo / "a.py"), self.owrite(self.repo / "a.py")),
            (self.write(self.outside), self.owrite(self.outside)),
            (self.write(self.home / ".config" / "x"), self.owrite(self.home / ".config" / "x")),
            (self.write("/tmp/policy-battery.txt"), self.owrite("/tmp/policy-battery.txt")),
            (self.write(self.repo / ".env"), self.owrite(self.repo / ".env")),
            (self.read(ROOT / "src" / "policy.py"), self.oread(ROOT / "src" / "policy.py")),
            (self.write(ROOT / "src" / "policy.py"), self.owrite(ROOT / "src" / "policy.py")),
            (self.bash("git status"), self.obash("git status")),
            (self.bash("git commit -m x"), self.obash("git commit -m x")),
            (self.bash("git push"), self.obash("git push")),
            (self.bash("python3 -m unittest discover -s tests"),
             self.obash("python3 -m unittest discover -s tests")),
            (self.bash("curl https://example.com"), self.obash("curl https://example.com")),
            (self.bash("pip install requests"), self.obash("pip install requests")),
            (self.bash("sudo ls"), self.obash("sudo ls")),
            (self.bash("python3 -c 'import os'"), self.obash("python3 -c 'import os'")),
            (self.bash("rm -rf $HOME"), self.obash("rm -rf $HOME")),
            (self.bash("cat *"), self.obash("cat *")),
            (self.bash("grep x .env a.txt"), self.obash("grep x .env a.txt")),
            (self.bash("echo x >& /etc/foo"), self.obash("echo x >& /etc/foo")),
            (self.bash("git show HEAD:.env"), self.obash("git show HEAD:.env")),
            (self.bash("cat ~/.config/gh/hosts.yml"), self.obash("cat ~/.config/gh/hosts.yml")),
            (self.bash("sort -k SUPERSECRET file.txt"), self.obash("sort -k SUPERSECRET file.txt")),
            (self.search("select:SendMessage,ExitPlanMode"),
             self.osearch("select:SendMessage,ExitPlanMode")),
            (self.search("select:Bash"), self.osearch("select:Bash")),
            ({"tool_name": "TodoWrite", "tool_input": {}}, {"tool_name": "todowrite", "tool_input": {}}),
        ]

    def test_claude_and_opencode_classify_the_same_battery_the_same_way(self):
        for claude_event, opencode_event in self._battery():
            with self.subTest(tool=claude_event["tool_name"]):
                claude = self.decide(claude_event)
                opencode = self.decide(opencode_event)
                self.assertEqual(claude["decision"], opencode["decision"])
                self.assertEqual(claude["capability"], opencode["capability"])
                self.assertEqual(claude["reason"], opencode["reason"])

    def test_node_plugin_calls_the_same_cli_for_the_same_battery(self):
        if NODE is None:
            self.skipTest("node is not available")
        pairs = self._battery()
        claude_events = [pair[0] for pair in pairs]
        opencode_events = [pair[1] for pair in pairs]
        expected = [self.decide(event) for event in claude_events]
        env = self.env(WS_POLICY_CLI=str(POLICY_CLI))
        script = """
const plugin = require(process.argv[1]);
const events = JSON.parse(process.argv[2]);
const options = {env: JSON.parse(process.argv[3])};
process.stdout.write(JSON.stringify(plugin.decideMany(events, options)));
"""
        process = subprocess.run([NODE, "-e", script, str(PLUGIN), json.dumps(opencode_events),
                                  json.dumps(env)], capture_output=True, text=True, check=False)
        self.assertEqual(process.returncode, 0, process.stderr)
        actual = json.loads(process.stdout)
        self.assertEqual(len(actual), len(expected))
        for wanted, got, event in zip(expected, actual, opencode_events):
            with self.subTest(event=event):
                self.assertEqual(got["decision"], wanted["decision"])
                self.assertEqual(got["capability"], wanted["capability"])
                self.assertEqual(got["reason"], wanted["reason"])

    def test_hook_process_and_node_plugin_agree_on_the_same_battery(self):
        if NODE is None:
            self.skipTest("node is not available")
        pairs = self._battery()
        hook_decisions = []
        for claude_event, _opencode_event in pairs:
            output = self.hook(claude_event)
            decision = "allow" if output is None else output["hookSpecificOutput"]["permissionDecision"]
            hook_decisions.append(decision)
        opencode_events = [pair[1] for pair in pairs]
        env = self.env(WS_POLICY_CLI=str(POLICY_CLI))
        script = """
const plugin = require(process.argv[1]);
const events = JSON.parse(process.argv[2]);
const options = {env: JSON.parse(process.argv[3])};
process.stdout.write(JSON.stringify(plugin.decideMany(events, options)));
"""
        process = subprocess.run([NODE, "-e", script, str(PLUGIN), json.dumps(opencode_events),
                                  json.dumps(env)], capture_output=True, text=True, check=False)
        self.assertEqual(process.returncode, 0, process.stderr)
        plugin_decisions = [entry["decision"] for entry in json.loads(process.stdout)]
        self.assertEqual(hook_decisions, plugin_decisions)

    def test_node_plugin_blocks_deny_and_ask(self):
        if NODE is None:
            self.skipTest("node is not available")
        env = self.env(WS_POLICY_CLI=str(POLICY_CLI))
        script = """
const plugin = require(process.argv[1]);
const options = {env: JSON.parse(process.argv[2])};
const result = {allow: null, deny: null, ask: null};
for (const [key, command] of [["deny", "sudo ls"], ["ask", "git push"], ["allow", "git status"]]) {
  try {
    result[key] = plugin.before({tool: "bash", sessionID: "s"}, {args: {command}}, options);
  } catch (error) {
    result[key] = {error: true, policy: error.policy};
  }
}
process.stdout.write(JSON.stringify(result));
"""
        process = subprocess.run([NODE, "-e", script, str(PLUGIN), json.dumps(env)],
                                 capture_output=True, text=True, check=False)
        self.assertEqual(process.returncode, 0, process.stderr)
        result = json.loads(process.stdout)
        self.assertTrue(result["deny"]["error"])
        self.assertEqual(result["deny"]["policy"]["decision"], "deny")
        self.assertTrue(result["ask"]["error"])
        self.assertEqual(result["ask"]["policy"]["decision"], "ask")
        self.assertEqual(result["allow"]["decision"], "allow")

    # --- installation --------------------------------------------------------------------------
    def test_harness_files_lists_the_policy_artifacts(self):
        names = installer.harness_files(self.repo)
        for name in ("policy.yaml", "opencode-policy.js", "policy.py", "yaml_min.py",
                     "i18n/pt-BR.json"):
            self.assertIn(name, names)

    def test_policy_installation_is_idempotent_and_self_contained(self):
        target = self.root / "installed"
        first = installer.install_policy_files(target)
        self.assertEqual(first, ["policy.yaml", "opencode-policy.js", "policy.py", "yaml_min.py",
                                 "i18n/pt-BR.json"])
        snapshot = {name: (target / name).read_bytes() for name in first}
        second = installer.install_policy_files(target)
        self.assertEqual(first, second)
        for name in first:
            self.assertEqual((target / name).read_bytes(), snapshot[name])
        self.assertEqual(stat.S_IMODE((target / "policy.py").stat().st_mode) & 0o700,
                         stat.S_IMODE((SRC / "policy.py").stat().st_mode) & 0o700)

        # The copied engine finds its own policy.yaml and the copied YAML reader.
        process = subprocess.run([sys.executable, str(target / "policy.py"), "check"],
                                 input=json.dumps(self.bash("git status")),
                                 env=self.env(WS_POLICY=None), capture_output=True, text=True,
                                 check=False)
        self.assertEqual(process.returncode, 0, process.stderr)
        self.assertEqual(json.loads(process.stdout)["decision"], "allow")

        if NODE is not None:
            script = """
const plugin = require(process.argv[1]);
const env = JSON.parse(process.argv[2]);
process.stdout.write(JSON.stringify({
  decision: plugin.decide(
    {tool_name: "bash", tool_input: {command: "git status"}}, {env}),
  message: plugin.translate("policy.reason.deny", "admin", env),
}));
"""
            env = self.env(WS_POLICY=None, WS_POLICY_CLI=None)
            node = subprocess.run([NODE, "-e", script, str(target / "opencode-policy.js"),
                                   json.dumps(env)], capture_output=True, text=True, check=False)
            self.assertEqual(node.returncode, 0, node.stderr)
            installed = json.loads(node.stdout)
            self.assertEqual(installed["decision"]["decision"], "allow")
            self.assertEqual(installed["message"], i18n.t("policy.reason.deny", capability="admin"))


if __name__ == "__main__":
    unittest.main()
