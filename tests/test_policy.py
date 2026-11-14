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
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
SECURITY = ROOT / "security"
POLICY_CLI = SRC / "policy.py"
PLUGIN = SECURITY / "opencode-policy.js"
NODE = shutil.which("node")

sys.path.insert(0, str(SRC))

import i18n  # noqa: E402
import installer  # noqa: E402
import policy  # noqa: E402
import yaml_min  # noqa: E402

EXPECTED_CAPABILITIES = {
    "repo.read", "repo.write", "repo.outside",
    "vcs.read", "vcs.write", "vcs.remote",
    "test.run", "net", "net.web", "install", "admin",
    "config.read", "config.write", "tmp", "secrets", "session.external",
    "workspace.read", "workspace.write",
}

ENV_KEYS = (
    "HOME", "WS_STATE", "WS_SESSION_ROLE", "WS_SESSION_ROOT", "WS_SESSION_PARENT_ROLE",
    "WS_PARENT_ROLE", "WS_SESSION_ID", "WS_POLICY", "WS_POLICY_AUTO", "WS_AUTO", "WS_DIR",
    "WS_I18N", "WS_POLICY_CLI", "TMPDIR", "WS_POLICY_ASK", "WS_POLICY_OPEN", "AT_WS_CONFIG",
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
        document = yaml_min.loads((SECURITY / "policy.yaml").read_text(encoding="utf-8"))
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
        document = yaml_min.loads((SECURITY / "policy.yaml").read_text(encoding="utf-8"))
        self.assertEqual(document["default"], "deny")
        self.assertEqual(set(document["roles"]), {"repo", "home", "self", "subagent"})
        self.assertEqual(set(policy.CAPABILITIES), EXPECTED_CAPABILITIES)
        for role in ("repo", "home"):
            self.assertEqual(set(document["roles"][role]), EXPECTED_CAPABILITIES)
            for value in document["roles"][role].values():
                self.assertIn(value, policy.DECISIONS)
        self.assertEqual(document["roles"]["self"], {"inherits": "repo"})
        self.assertEqual(document["roles"]["subagent"], {"inherits": "parent"})

    def test_policy_file_loads_through_the_engine(self):
        document = policy.load_policy(SECURITY / "policy.yaml")
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
            (self.bash("curl https://example.com"), "net", "allow"),
            (self.bash("pip install requests"), "install", "allow"),
            (self.bash("sudo ls"), "admin", "privileged"),
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
                (self.bash("sudo ls"), "admin", "privileged"),
                (self.read(self.home / ".env"), "secrets", "deny"),
                ({"tool_name": "send_message", "tool_input": {}}, "session.external", "ask"),
            ]
            for event, capability, decision in cases:
                with self.subTest(event=event):
                    result = policy.check_event(event)
                    self.assertEqual(result["capability"], capability)
                    self.assertEqual(result["decision"], decision)

    # --- web reads (item 157) ------------------------------------------------------------------
    def web(self, tool="WebFetch", **extra):
        return {"tool_name": tool, "tool_input": {"url": "https://example.com", "query": "x"},
                "session_id": "sess-1", **extra}

    def test_web_reads_are_net_web_allowed_for_every_role(self):
        # WebFetch/WebSearch/fetch are net.web (not net), allow in every role, so a subagent of
        # the home session no longer stops on `ask`.
        for tool in ("WebFetch", "WebSearch", "fetch"):
            with self.subTest(tool=tool, role="repo"):
                result = self.decide(self.web(tool))
                self.assertEqual((result["capability"], result["decision"]),
                                 ("net.web", "allow"))
        for role, root in (("home", self.home), ("self", self.repo)):
            with self.subTest(role=role):
                with patched_env(WS_SESSION_ROLE=role, WS_SESSION_ROOT=str(root)):
                    for tool in ("WebFetch", "WebSearch"):
                        result = policy.check_event(self.web(tool))
                        self.assertEqual((result["capability"], result["decision"]),
                                         ("net.web", "allow"))
        with patched_env(WS_SESSION_ROLE="home", WS_SESSION_ROOT=str(self.home)):
            result = policy.check_event(self.web("WebFetch", agent_id="a1"))
            self.assertEqual((result["capability"], result["decision"]), ("net.web", "allow"))
            result = policy.check_event(self.web("WebSearch", agent_id="a1"))
            self.assertEqual((result["capability"], result["decision"]), ("net.web", "allow"))
            # the shell network tools keep their own capability and their own answer
            for command, decision in (("curl https://example.com", "ask"),
                                      ("gh pr list", "ask")):
                with self.subTest(command=command):
                    result = policy.check_event(self.bash(command, agent_id="a1"))
                    self.assertEqual((result["capability"], result["decision"]),
                                     ("net", decision))
        # repo keeps `curl` and `gh` on net (allow), not net.web
        self.assertEqual(self.decide(self.bash("gh pr list"))["capability"], "net")
        self.assertEqual(self.decide(self.bash("gh pr list"))["decision"], "allow")

    def test_subagent_tightening_of_net_web_is_honored(self):
        fixture = self.fixture_policy(subagent={"net.web": "deny"})
        with patched_env(WS_POLICY=str(fixture), WS_SESSION_ROLE="home",
                         WS_SESSION_ROOT=str(self.home)):
            result = policy.check_event(self.web("WebFetch", agent_id="a1"))
            self.assertEqual((result["capability"], result["decision"]), ("net.web", "deny"))

    def test_table_without_net_web_fails_closed(self):
        for role in ("repo", "home"):
            with self.subTest(role=role):
                broken = yaml_min.loads((SECURITY / "policy.yaml").read_text(encoding="utf-8"))
                del broken["roles"][role]["net.web"]
                path = self.root / f"no-net-web-{role}.yaml"
                path.write_text(yaml_min.dumps(broken), encoding="utf-8")
                with self.assertRaises(policy.PolicyError) as raised:
                    policy.load_policy(path)
                self.assertEqual(raised.exception.reason, "policy.reason.invalid")
                with patched_env(WS_POLICY=str(path)):
                    result = policy.check_event(self.web("WebFetch"))
                self.assertEqual((result["decision"], result["reason"]),
                                 ("deny", "policy.reason.invalid"))

    def test_net_web_deny_is_never_upgraded_by_auto_or_ask_mode(self):
        fixture = self.fixture_policy(repo={"net.web": "deny"}, home={"net.web": "deny"})
        self.ask_marker()
        for role, root in (("repo", self.repo), ("home", self.home)):
            for overrides in ({"WS_POLICY_AUTO": "1"}, {}):
                with self.subTest(role=role, auto=bool(overrides)):
                    with patched_env(WS_POLICY=str(fixture), WS_SESSION_ROLE=role,
                                     WS_SESSION_ROOT=str(root), **overrides):
                        result = policy.check_event(self.web("WebFetch"))
                        self.assertEqual((result["capability"], result["decision"]),
                                         ("net.web", "deny"))
        # `ask` for net.web is not a box capability, so `auto` does not turn it into allow
        ask_fixture = self.fixture_policy(home={"net.web": "ask"})
        with patched_env(WS_POLICY=str(ask_fixture), WS_SESSION_ROLE="home",
                         WS_SESSION_ROOT=str(self.home), WS_POLICY_AUTO="1"):
            result = policy.check_event(self.web("WebFetch"))
            self.assertEqual(result["decision"], "ask")

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
            self.assertEqual(policy.workspace_root(), str(ROOT))
            write = policy.check_event(self.write(ROOT / "src" / "policy.py"))
            self.assertEqual((write["decision"], write["capability"]),
                             ("deny", "workspace.write"))
            read = policy.check_event(self.read(ROOT / "src" / "policy.py"))
            self.assertEqual((read["decision"], read["capability"]),
                             ("allow", "workspace.read"))

    # --- the control plane (item 72) -----------------------------------------------------------
    def test_control_plane_is_denied_for_every_role_before_the_table(self):
        control = self.home / ".config" / "ai-tmux-workspace"
        targets = (control / "config.yaml", control / "state" / "policy.jsonl",
                   control / "conference" / "log.json")
        roles = (
            {"WS_SESSION_ROLE": "repo", "WS_SESSION_ROOT": str(self.repo)},
            {"WS_SESSION_ROLE": "home", "WS_SESSION_ROOT": str(self.home)},
            {"WS_SESSION_ROLE": "self", "WS_SESSION_ROOT": str(self.repo)},
            {"WS_SESSION_ROLE": "subagent", "WS_SESSION_PARENT_ROLE": "home",
             "WS_SESSION_ROOT": str(self.home)},
        )
        for role_env in roles:
            with self.subTest(role=role_env["WS_SESSION_ROLE"]):
                with patched_env(**role_env):
                    for target in targets:
                        with self.subTest(target=target):
                            result = policy.check_event(self.write(target))
                            self.assertEqual(
                                (result["decision"], result["capability"], result["reason"]),
                                ("deny", "control_plane.write", "policy.reason.control_plane"))
                            # Reading the control plane stays config.read for every role.
                            read = policy.check_event(self.read(target))
                            self.assertEqual((read["decision"], read["capability"]),
                                             ("allow", "config.read"))

    def test_installed_harness_and_code_stay_readable_by_a_repo_session(self):
        # Item 72 exception: the installed code and HARNESS.md are outside the control plane and
        # readable by any role, while a write to them keeps the role-table decision.
        installed = self.home / ".local" / "share" / "ai-tmux-workspace"
        harness = installed / "harness" / "HARNESS.md"
        read = self.decide(self.read(harness))
        self.assertEqual((read["decision"], read["capability"]), ("allow", "config.read"))
        code = self.decide(self.read(ROOT / "src" / "policy.py"))
        self.assertEqual((code["decision"], code["capability"]), ("allow", "workspace.read"))
        write = self.decide(self.write(harness))
        self.assertEqual((write["decision"], write["capability"]), ("deny", "config.write"))

    def test_control_plane_blocks_the_shell_forms_that_write(self):
        control = self.home / ".config" / "ai-tmux-workspace"
        commands = (
            f"echo x > {control}/config.yaml",
            f"cp /tmp/a.txt {control}/config.yaml",
            f"rm -rf {control}",
            f"sed -i s/x/y/ {control}/config.yaml",
            f"tee {control}/state/policy.jsonl",
            f"mv /tmp/a.txt {control}/conference/log.json",
        )
        for command in commands:
            with self.subTest(command=command):
                result = self.decide(self.bash(command))
                self.assertEqual(
                    (result["decision"], result["capability"], result["reason"]),
                    ("deny", "control_plane.write", "policy.reason.control_plane"))

    def test_control_plane_wins_over_the_declared_root_auto_and_ask_mode(self):
        control = self.home / ".config" / "ai-tmux-workspace"
        target = control / "config.yaml"
        # A declared root inside the control plane does not reopen it.
        with patched_env(WS_SESSION_ROLE="repo", WS_SESSION_ROOT=str(control)):
            self.assertEqual(self.decide(self.write(target))["capability"],
                             "control_plane.write")
        # Neither `auto` nor the opt-in ask mode turns the barrier into an allow/ask.
        with patched_env(WS_POLICY_AUTO="1"):
            self.assertEqual(self.decide(self.write(target))["decision"], "deny")
        with patched_env(WS_POLICY_ASK="1"):
            result = self.decide(self.write(target))
            self.assertEqual((result["decision"], result["reason"]),
                             ("deny", "policy.reason.control_plane"))
        # The rest of ~/.config is not the control plane: home still has config.write there.
        with patched_env(WS_SESSION_ROLE="home", WS_SESSION_ROOT=str(self.repo)):
            result = self.decide(self.write(self.home / ".config" / "app.conf"))
            self.assertEqual((result["decision"], result["capability"]),
                             ("allow", "config.write"))

    def test_control_plane_follows_at_ws_config(self):
        folder = self.root / "custom-config"
        folder.mkdir()
        with patched_env(AT_WS_CONFIG=str(folder / "config.yaml"),
                         WS_SESSION_ROLE="home", WS_SESSION_ROOT=str(self.repo)):
            result = self.decide(self.write(folder / "state" / "policy.jsonl"))
            self.assertEqual((result["decision"], result["capability"]),
                             ("deny", "control_plane.write"))
            # The default folder is now just another path under ~/.config (the home table allows it).
            result = self.decide(self.write(self.home / ".config" / "ai-tmux-workspace" / "config.yaml"))
            self.assertEqual((result["decision"], result["capability"]),
                             ("allow", "config.write"))

    # --- the dev role `self` (item 85) ---------------------------------------------------------
    def workspace_code(self):
        """A fake workspace clone with one source file, as WS_DIR points to it."""
        code = self.root / "workspace-code"
        (code / "src").mkdir(parents=True, exist_ok=True)
        target = code / "src" / "policy.py"
        target.write_text("x", encoding="utf-8")
        return code, target

    def test_self_role_inherits_the_repo_table(self):
        document = policy.load_policy(SECURITY / "policy.yaml")
        self.assertEqual(policy._role_table(document, "self"), policy._role_table(document, "repo"))
        self.assertEqual(policy._effective_capabilities(document, "self", False),
                         policy._effective_capabilities(document, "repo", False))

    def test_self_role_writes_the_workspace_code_from_its_own_box(self):
        code, target = self.workspace_code()
        with patched_env(WS_DIR=str(code), WS_SESSION_ROLE="self", WS_SESSION_ROOT=str(code)):
            for event in (self.write(target), self.bash(f"echo x > {target}")):
                with self.subTest(event=event):
                    result = policy.check_event(event)
                    self.assertEqual(
                        (result["decision"], result["capability"], result["reason"]),
                        ("allow", "workspace.write", "policy.reason.workspace_self"))
            self.assertEqual(policy.check_event(self.read(target))["decision"], "allow")
            # the rest of the repo table still applies inside the dev box
            cases = [
                (self.write(code / ".env"), "secrets", "deny"),
                (self.write(self.outside), "repo.outside", "deny"),
                (self.write(self.home / ".config" / "app.conf"), "config.write", "deny"),
                (self.bash("curl https://example.com"), "net", "allow"),
                (self.bash("pip install requests"), "install", "allow"),
                (self.bash("sudo ls"), "admin", "privileged"),
                (self.bash("git push origin main"), "vcs.remote", "ask"),
                (self.bash("git status"), "vcs.read", "allow"),
            ]
            for event, capability, decision in cases:
                with self.subTest(event=event):
                    result = policy.check_event(event)
                    self.assertEqual((result["capability"], result["decision"]),
                                     (capability, decision))

    def test_repo_sibling_stays_denied_from_the_workspace_code(self):
        code, target = self.workspace_code()
        with patched_env(WS_DIR=str(code), WS_SESSION_ROLE="repo", WS_SESSION_ROOT=str(self.repo)):
            for event in (self.write(target), self.bash(f"echo x > {target}")):
                with self.subTest(event=event):
                    result = policy.check_event(event)
                    self.assertEqual((result["decision"], result["capability"]),
                                     ("deny", "workspace.write"))
            self.assertEqual(policy.check_event(self.write(self.repo / "a.py"))["decision"],
                             "allow")

    def test_self_role_outside_the_workspace_code_does_not_open_it(self):
        code, target = self.workspace_code()
        with patched_env(WS_DIR=str(code), WS_SESSION_ROLE="self", WS_SESSION_ROOT=str(self.repo)):
            result = policy.check_event(self.write(target))
            self.assertEqual((result["decision"], result["capability"]),
                             ("deny", "workspace.write"))

    def test_unknown_role_stays_denied_even_inside_the_workspace_code(self):
        code, target = self.workspace_code()
        for role in ("dev", "Self-ish", "admin"):
            with self.subTest(role=role):
                with patched_env(WS_DIR=str(code), WS_SESSION_ROLE=role,
                                 WS_SESSION_ROOT=str(code)):
                    result = policy.check_event(self.write(target))
                    self.assertEqual((result["decision"], result["reason"]),
                                     ("deny", "policy.reason.unknown_role"))

    def test_subagent_of_the_dev_session_takes_the_self_table(self):
        code, target = self.workspace_code()
        with patched_env(WS_DIR=str(code), WS_SESSION_ROLE="subagent",
                         WS_SESSION_PARENT_ROLE="self", WS_SESSION_ROOT=str(code)):
            result = policy.check_event(self.write(target, agent_id="agent-1"))
            self.assertEqual((result["decision"], result["capability"]),
                             ("allow", "workspace.write"))
        with patched_env(WS_DIR=str(code), WS_SESSION_ROLE="subagent",
                         WS_SESSION_PARENT_ROLE="dev", WS_SESSION_ROOT=str(code)):
            result = policy.check_event(self.write(target, agent_id="agent-1"))
            self.assertEqual((result["decision"], result["reason"]),
                             ("deny", "policy.reason.no_parent"))

    def test_self_entry_is_only_the_repo_inheritance(self):
        # a self role that grants anything on its own is an invalid policy: fail closed
        document = yaml_min.loads((SECURITY / "policy.yaml").read_text(encoding="utf-8"))
        document["roles"]["self"]["net"] = "allow"
        path = self.root / "self-policy.yaml"
        path.write_text(yaml_min.dumps(document), encoding="utf-8")
        with self.assertRaises(policy.PolicyError) as raised:
            policy.load_policy(path)
        self.assertEqual(raised.exception.reason, "policy.reason.invalid")

    # --- fail closed ---------------------------------------------------------------------------
    def test_unknown_tools_and_ambiguous_forms_defer_instead_of_failing_closed(self):
        # Item 1xx: what the engine cannot place is not denied; the session's mode decides.
        cases = [
            {"tool_name": "mcp__server__tool", "tool_input": {}},
            {"tool_name": "UnknownTool", "tool_input": {}},
            self.bash("python3 -c 'import os'"),
            self.bash("sh -c 'rm -rf /'"),
            self.bash("git"),
            self.bash("rm"),
            self.bash("cat $(echo /etc/passwd)"),
            self.bash("find . -delete"),
        ]
        for event in cases:
            with self.subTest(event=event):
                result = self.decide(event)
                self.assertEqual((result["decision"], result["reason"]),
                                 ("allow", "policy.reason.defer"))
                self.assertIsNone(result["capability"])
        # A secret named by an option value is still a secret, whatever the form.
        for command in ("diff --from-file=.env a.txt", "diff --to-file=.env a.txt",
                        "wc --files0-from=.env", "grep --exclude-from=.env x a.txt",
                        "rg --ignore-file=.env x .", "tree --fromfile=.env"):
            with self.subTest(command=command):
                result = self.decide(self.bash(command))
                self.assertEqual((result["decision"], result["capability"]), ("deny", "secrets"))
        # A file tool without a path is still an unplaceable file action: fail closed.
        result = self.decide({"tool_name": "Write", "tool_input": {}})
        self.assertEqual((result["decision"], result["capability"]), ("deny", None))

    def test_admin_in_an_ambiguous_segment_stays_denied(self):
        # The engine cannot place these forms, but an admin program named in them still denies,
        # wherever it sits: a known segment, a substitution, a wrapper, a quoted script.
        commands = (
            "grep -r x . ; sudo ls",
            "find . -exec sudo rm {} \\;",
            "find . -exec /usr/bin/sudo rm {} \\;",
            "xargs sudo rm",
            "sh -c 'sudo id'",
            "bash -c \"pkexec ls\"",
            "eval su -c ls",
            "python3 -c \"import os; os.system('sudo id')\"",
            "echo $(sudo id)",
            "echo `doas id`",
            "cat $(echo /etc/passwd) && sudo ls",
            "cd $D && sudo ls",
            "cd /x && sudo ls",
            "sed -e 's/x/y/' a.txt && sudo ls",
            "grep -r x . ; echo $(sudo id)",
            "echo \"$(sudo id)\"",
            "time sudo ls",
        )
        for command in commands:
            with self.subTest(command=command):
                result = self.decide(self.bash(command))
                self.assertEqual((result["decision"], result["capability"]), ("privileged", "admin"))
        # A bare admin name used as an argument is not a command: the form defers.
        for command in ("grep -r passwd src", "grep -r service .", "grep -r \"mount\" docs"):
            with self.subTest(command=command):
                result = self.decide(self.bash(command))
                self.assertEqual((result["decision"], result["reason"]),
                                 ("allow", "policy.reason.defer"))
        # The path of a program is not the program: `/etc/passwd` is a path, not `passwd`.
        result = self.decide(self.bash("sort --random-source=/etc/passwd file"))
        self.assertEqual(result["decision"], "allow")

    # --- regression: shell expansion, globs and ambiguous paths --------------------------------
    def test_shell_expansion_and_globs_defer_unless_they_name_a_barrier(self):
        # A glob or an expansion the engine cannot resolve defers (item 1xx); the barriers still
        # apply to what the text names: a secret glob, a secret or a workspace write.
        commands = [
            "rm -rf $HOME",
            "rm -rf ${HOME}/x",
            "echo x > $HOME/file",
            "tee $HOME/x",
            "cat *",
            "rm *",
            "sed -i 's/x/y/' *.py",
            "awk '{print}' *.py",
            "grep x *.py",
            "cat ~zzz_nonexistent_user_zzz/file",
        ]
        for command in commands:
            with self.subTest(command=command):
                result = self.decide(self.bash(command))
                self.assertEqual((result["decision"], result["reason"]),
                                 ("allow", "policy.reason.defer"))
        for command, capability in (("cat .env*", "secrets"), ("git add '*.env'", "secrets"),
                                    ("cat ~/.ssh/*", "secrets"),
                                    ("cat $HOME/.ssh/id_rsa", "secrets")):
            with self.subTest(command=command):
                result = self.decide(self.bash(command))
                self.assertEqual((result["decision"], result["capability"]), ("deny", capability))
        # A glob inside the workspace code is a workspace write, whatever the form (the `*` is in
        # the directory the `cd` left).
        workspace = policy.workspace_root()
        if workspace:
            for command in (f"rm -rf {workspace}/src/*", f"cd {workspace} && rm -rf *"):
                with self.subTest(command=command):
                    result = self.decide(self.bash(command))
                    self.assertEqual((result["decision"], result["capability"]),
                                     ("deny", "workspace.write"))

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

    def test_unknown_programs_with_scripts_or_expansions_defer(self):
        # Item 135 and the ambiguity rule: a script with an embedded path or an expansion is not
        # resolvable, so it defers whatever the program is; no path is decided from its text.
        commands = [
            "sed -e 's/x/y/w /etc/foo' a.txt",
            "awk 'BEGIN{print \"x\" > \"/etc/foo\"}'",
            "awk '{print $1}' file.txt",
            "gawk '{print}' file.txt",
            "mawk '{print}' file.txt",
        ]
        for command in commands:
            with self.subTest(command=command):
                result = self.decide(self.bash(command))
                self.assertEqual((result["decision"], result["reason"]),
                                 ("allow", "policy.reason.defer"))

    def test_unknown_programs_defer_and_keep_the_path_barrier_on_their_arguments(self):
        # Inside the box: no path escape, so the session mode decides (allow, defer).
        for command in ("sed -e 's/x/y/' a.txt", "less file.txt", "more file.txt"):
            with self.subTest(command=command):
                self.assertEqual(self.decide(self.bash(command))["decision"], "allow")
        # Outside the box or in a secret: the argument's own barrier applies.
        for command, capability in (("less /etc/passwd", "repo.outside"),
                                    ("more .env", "secrets"),
                                    ("less --file=/etc/passwd", "repo.outside")):
            with self.subTest(command=command):
                result = self.decide(self.bash(command))
                self.assertEqual((result["decision"], result["capability"]), ("deny", capability))

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

    def test_macos_secret_locations_are_secrets_in_the_home_role(self):
        # APFS is case-insensitive: a different spelling names the same file, so the comparison is
        # case-folded (`~/.SSH` is `~/.ssh`). The macOS credential stores are secrets too.
        paths = (
            self.home / ".SSH" / "id_rsa",
            self.home / ".SSH" / "config",
            self.home / "LIBRARY" / "Keychains" / "login.keychain-db",
            self.home / "Library" / "Keychains" / "login.keychain-db",
            self.home / "Library" / "Application Support" / "Claude" / "claude_desktop_config.json",
            self.home / "Library" / "Cookies" / "Cookies.binarycookies",
            self.home / "Library" / "Messages" / "chat.db",
            self.home / "Library" / "Mail" / "x.emlx",
            self.home / "Library" / "Safari" / "History.db",
        )
        with mock.patch.object(policy, "_is_darwin", return_value=True):
            with patched_env(WS_SESSION_ROLE="home", WS_SESSION_ROOT=str(self.home)):
                for value in paths:
                    with self.subTest(path=value):
                        for event in (self.read(value), self.write(value)):
                            result = policy.check_event(event)
                            self.assertEqual((result["decision"], result["capability"]),
                                             ("deny", "secrets"))
                # The same files named through `~` and with a different case are secrets as well.
                for command in ("cat ~/.SSH/id_rsa", "cat ~/.SSH/config",
                                "cat ~/library/keychains/login.keychain-db"):
                    with self.subTest(command=command):
                        result = policy.check_event(self.bash(command))
                        self.assertEqual((result["decision"], result["capability"]),
                                         ("deny", "secrets"))
                # The rest of `~/Library` is not a secret store.
                result = policy.check_event(
                    self.read(self.home / "Library" / "Preferences" / "x.plist"))
                self.assertEqual(result["capability"], "repo.read")
        # The case-folded form is only for comparison: the logged target keeps the given path.
        target = self.home / "Library" / "Keychains" / "Login.KEYCHAIN-DB"
        self.assertEqual(self.cli(self.read(target))["decision"], "deny")
        entry = json.loads((self.state / "policy.jsonl").read_text(encoding="utf-8")
                           .splitlines()[-1])
        self.assertEqual(entry["target"], str(target))

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

    def test_macos_temp_root_is_tmp_only_on_darwin(self):
        # `tempfile.gettempdir()` on macOS returns a per-session directory under `/var/folders`
        # (real path `/private/var/folders`). Both spellings are inside the box there; on Linux
        # they stay outside it. The roots are fixed strings, never the session's TMPDIR.
        real = "/private/var/folders/ab/cd0x/T/policy-test.txt"
        symlinked = "/var/folders/ab/cd0x/T/policy-test.txt"
        with mock.patch.object(policy, "_is_darwin", return_value=False):
            self.assertNotIn("/private/var/folders", policy._tmp_roots())
            self.assertNotIn("/var/folders", policy._tmp_roots())
            self.assertIn("/tmp", policy._tmp_roots())
            for value in (real, symlinked):
                with self.subTest(path=value, platform="linux"):
                    result = self.decide(self.write(value))
                    self.assertEqual((result["decision"], result["capability"]),
                                     ("deny", "repo.outside"))
        with mock.patch.object(policy, "_is_darwin", return_value=True):
            self.assertIn("/private/var/folders", policy._tmp_roots())
            self.assertIn("/var/folders", policy._tmp_roots())
            for value in (real, symlinked):
                with self.subTest(path=value, platform="darwin"):
                    for event in (self.write(value), self.obash(f"echo x > {value}")):
                        result = self.decide(event)
                        self.assertEqual((result["decision"], result["capability"]),
                                         ("allow", "tmp"))
            # `/tmp` is still the temp area on macOS.
            self.assertEqual(self.decide(self.write("/tmp/policy-test.txt"))["capability"], "tmp")
            # TMPDIR still does not enlarge the box, on macOS either.
            with patched_env(TMPDIR="/var/tmp/elsewhere"):
                result = self.decide(self.write("/var/tmp/ai-tmux-workspace-elsewhere/x"))
                self.assertEqual((result["decision"], result["capability"]),
                                 ("deny", "repo.outside"))

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

    def test_find_write_and_symlink_options_defer(self):
        # find's write and symlink options are ambiguous forms now: they defer to the session mode,
        # and the path they name is not decided by the engine (item 1xx).
        for command in ("find . -fprint /etc/foo", "find . -fprint0 /etc/foo",
                        "find . -fprintf /etc/foo %p", "find . -fls /etc/foo",
                        "find -L .", "find -H .", "find . -follow"):
            with self.subTest(command=command):
                result = self.decide(self.bash(command))
                self.assertEqual((result["decision"], result["reason"]),
                                 ("allow", "policy.reason.defer"))
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

    def test_recursive_grep_and_hidden_rg_defer(self):
        # Recursive grep and hidden rg are ambiguous forms: they defer, they are not denied.
        for command in ("grep -r x .", "grep -R x .", "grep --recursive x .",
                        "grep --dereference-recursive x .", "grep -rn x .",
                        "grep -d recurse x .", "grep -drecurse x .",
                        "rg --hidden x .", "rg -u x .", "rg -uu x .",
                        "rg --unrestricted x .", "rg --no-ignore x .",
                        "rg --no-ignore-vcs x .", "rg -L x .", "rg --follow x ."):
            with self.subTest(command=command):
                result = self.decide(self.bash(command))
                self.assertEqual((result["decision"], result["reason"]),
                                 ("allow", "policy.reason.defer"))
        for command in ("rg x .", "grep -er x .", "grep x .", "rg -e-u x ."):
            with self.subTest(command=command):
                self.assertEqual(self.decide(self.bash(command))["decision"], "allow")

    def test_dangerous_read_program_options_defer_but_secrets_still_deny(self):
        for command in ("sort --compress-program=sh file", "sort -T /tmp file",
                        "sort --temporary-directory=/tmp file", "sort -nT/tmp file",
                        "file -C", "file --compile file", "file -m /etc/magic file",
                        "sort --random-source=/etc/passwd file"):
            with self.subTest(command=command):
                result = self.decide(self.bash(command))
                self.assertEqual((result["decision"], result["reason"]),
                                 ("allow", "policy.reason.defer"))
        for command, capability in (("sort --files0-from=.env file", "secrets"),
                                    ("file -f .env", "secrets"),
                                    ("file --files-from=.env", "secrets")):
            with self.subTest(command=command):
                result = self.decide(self.bash(command))
                self.assertEqual((result["decision"], result["capability"]), ("deny", capability))
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
                self.assertEqual(self.decide(self.bash("sudo ls"))["decision"], "privileged")
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
        # the parent repo allows net (item 135): the subagent inherits the allow
        self.assertEqual(self.decide(parent_repo["net"])["decision"], "allow")
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
            "admin": "allow",         # parent repo denies admin: ignored
            "repo.write": "deny",     # parent repo allows: accepted (stricter)
        })
        with patched_env(WS_POLICY=str(fixture)):
            self.assertEqual(self.decide(self.bash("sudo ls", agent_id="a1"))["decision"], "privileged")
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
        self.assertEqual(self.decide(self.bash("sudo ls"))["decision"], "privileged")
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

    def test_git_dash_c_outside_the_box_is_classified_and_ambiguous_defers(self):
        # `git -C` outside the box reanchors like any other `-C`: the subcommand decides (item 1xx).
        for command, capability in (("git -C /home/qualquer status", "vcs.read"),
                                    ("git -C ../outside status", "vcs.read"),
                                    ("git -C .claude/worktrees/x -C /home/qualquer status", "vcs.read")):
            with self.subTest(command=command):
                result = self.decide(self.bash(command))
                self.assertEqual((result["decision"], result["capability"]), ("allow", capability))
        # The forms the engine cannot place defer.
        for command in ("git -C", "git -C. status", "git --git-dir=.git status",
                        "git --work-tree=. status", "git -c core.pager=cat status"):
            with self.subTest(command=command):
                result = self.decide(self.bash(command))
                self.assertEqual((result["decision"], result["reason"]),
                                 ("allow", "policy.reason.defer"))
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

    def test_cd_outside_the_box_is_allowed_and_ambiguous_cd_defers(self):
        # A `cd` into a directory, inside or outside the box, is not a deny; the following segment
        # is classified as usual (`git status` is vcs.read whatever the directory).
        for command in ("cd .. && git status", "cd ../.. && git status",
                        "cd /home/qualquer && git status", "cd ~ && git status"):
            with self.subTest(command=command):
                result = self.decide(self.bash(command))
                self.assertEqual((result["decision"], result["capability"]), ("allow", "vcs.read"))
        # A `cd` the engine cannot place (no directory, `cd -`, flags, extra arguments, expansions,
        # globs) defers, and the rest of the line is still classified.
        for command in ("cd && git status", "cd - && git status",
                        "cd -- .claude/worktrees/x && git status",
                        "cd $HOME && git status", "cd *.d && git status",
                        "cd -a .claude/worktrees/x && git status",
                        "cd .claude/worktrees/x extra && git status"):
            with self.subTest(command=command):
                result = self.decide(self.bash(command))
                self.assertEqual((result["decision"], result["reason"]),
                                 ("allow", "policy.reason.defer"))

    def test_cd_and_git_dash_c_do_not_enter_secret_directories(self):
        with patched_env(WS_SESSION_ROLE="home", WS_SESSION_ROOT=str(self.home)):
            result = policy.check_event(self.bash("cd .ssh && git status"))
            self.assertEqual((result["decision"], result["capability"]), ("deny", "secrets"))
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
        # The anchor is local to one command: after a `cd` outside the box, a relative read in a new
        # call still resolves from the box root (repo.read), not from the directory left behind.
        self.assertEqual(self.decide(self.bash("cd /home/qualquer && cat x"))["capability"],
                         "repo.outside")
        self.assertEqual(self.decide(self.bash("cat x"))["capability"], "repo.read")

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
        self.assertEqual((result["decision"], result["capability"]), ("allow", "install"))
        # Arbitrary code (`-c`), a script and the other forms the table does not list defer.
        for command in ("python3 -c 'print(1)'", "python3 -I -c 'print(1)'",
                        "python3 -I -m unknown_module", "python3 -I -",
                        "python3 -I tests/run.py", "python3 -v -m unittest",
                        "python3 --help"):
            with self.subTest(command=command):
                result = self.decide(self.bash(command))
                self.assertEqual((result["decision"], result["reason"]),
                                 ("allow", "policy.reason.defer"))

    def test_python_venv_is_install_in_repo_and_ask_in_home(self):
        # `python -m venv` creates an environment and installs nothing; it is classified like
        # `pip install` (capability `install`, no target path), so the role table decides.
        for command in ("python3 -m venv .venv", "python -m venv .venv", "python3 -I -m venv .venv"):
            with self.subTest(command=command):
                result = self.decide(self.bash(command))
                self.assertEqual((result["decision"], result["capability"]), ("allow", "install"))
        with patched_env(WS_SESSION_ROLE="home", WS_SESSION_ROOT=str(self.home)):
            for command in ("python3 -m venv .venv", "python3 -I -m venv .venv"):
                with self.subTest(command=command, role="home"):
                    result = self.decide(self.bash(command))
                    self.assertEqual((result["decision"], result["capability"]), ("ask", "install"))

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
        # A path outside the box is the table's decision (repo.outside); a form the engine cannot
        # place (no --headless, an export, a missing value) defers.
        for command in ("godot --headless --path /home/qualquer",
                        "godot --headless --path=/home/qualquer",
                        "godot --headless --main-pack /etc/game.pck",
                        "godot --headless --script /etc/teste.gd",
                        "godot --headless -s /etc/teste.gd",
                        "godot --headless /etc/project.godot"):
            with self.subTest(command=command):
                result = self.decide(self.bash(command))
                self.assertEqual((result["decision"], result["capability"]), ("deny", "repo.outside"))
        for command in ("godot --quit", "godot4 --quit",
                        "godot --headless --export-release",
                        "godot --headless --export-debug",
                        "godot --headless --path"):
            with self.subTest(command=command):
                result = self.decide(self.bash(command))
                self.assertEqual((result["decision"], result["reason"]),
                                 ("allow", "policy.reason.defer"))
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
            ("git -C /home/qualquer status", "allow", "vcs.read"),
            ("cd .. && git status", "allow", "vcs.read"),
            ("python3 -c 'print(1)'", "allow", None),
            ("godot --quit", "allow", None),
            ("godot --headless --export-release", "allow", None),
        )
        for command, decision, capability in cases:
            with self.subTest(command=command):
                result = self.cli(self.bash(command))
                self.assertEqual((result["decision"], result["capability"]),
                                 (decision, capability))

    # --- ask mode: the person decides the unknown (item 119) -----------------------------------
    def ask_marker(self, name="policy.ask"):
        self.state.mkdir(parents=True, exist_ok=True)
        marker = self.state / name
        marker.write_text("", encoding="utf-8")
        return marker

    def test_ask_mode_is_off_without_the_marker_or_env(self):
        # Item 135: an unknown command defers to the session mode (allow, no reason of its own).
        for event in (self.bash("some-unknown-command --flag"), self.obash("other-unknown-command")):
            with self.subTest(event=event):
                result = self.decide(event)
                self.assertEqual((result["decision"], result["capability"]), ("allow", None))
                self.assertEqual(result["reason"], "policy.reason.defer")
        # An unknown tool name (not a shell command) defers the same way: the session's own
        # permission flow decides. A malformed event with no tool name still fails closed.
        unknown_tool = {"tool_name": "UnknownTool", "tool_input": {}, "session_id": "sess-1"}
        result = self.decide(unknown_tool)
        self.assertEqual((result["decision"], result["capability"]), ("allow", None))
        self.assertEqual(result["reason"], "policy.reason.defer")
        result = self.decide({"tool_input": {}, "session_id": "sess-1"})
        self.assertEqual((result["decision"], result["reason"]), ("deny", "policy.reason.unknown"))
        with patched_env(WS_POLICY_ASK="0"):
            self.assertEqual(self.decide(self.bash("some-unknown-command"))["reason"],
                             "policy.reason.defer")
        # A marker outside the declared state directory is not the person's marker.
        elsewhere = self.home / ".config" / "ai-tmux-workspace" / "state" / "policy.ask"
        elsewhere.parent.mkdir(parents=True)
        elsewhere.write_text("", encoding="utf-8")
        self.assertEqual(self.decide(self.bash("some-unknown-command"))["decision"], "allow")

    def test_unknown_program_defers_but_the_rest_of_the_command_is_still_classified(self):
        # A known segment in the same line keeps its own decision: `&&` and redirects are not
        # hidden by an unknown program (item 135).
        cases = (
            ("dotnet --version", "allow", "policy.reason.defer"),
            ("dotnet build && cat .env", "deny", "policy.reason.deny"),
            ("dotnet build && rm -rf ~/.config/ai-tmux-workspace", "deny",
             "policy.reason.control_plane"),
            ("dotnet build > ~/.config/ai-tmux-workspace/out", "deny",
             "policy.reason.control_plane"),
            ("dotnet build && sudo ls", "privileged", "policy.reason.privileged"),
            ("git status && dotnet --version", "allow", "policy.reason.allow"),
        )
        for command, decision, reason in cases:
            with self.subTest(command=command):
                result = self.decide(self.bash(command))
                self.assertEqual((result["decision"], result["reason"]), (decision, reason))

    def test_toolchain_forms_it_does_not_know_defer_and_keep_the_path_barrier(self):
        # Item 135: `cargo run`, `go run`, `make` without a target or with `-C`, `npm run` and the
        # other toolchains defer to the session mode; their path arguments still meet the barrier.
        defer = (
            "cargo run", "cargo run --bin app", "cargo run -- --port 8080", "go run main.go",
            "go run ./cmd/app", "make", "make deploy", "make -C sub", "make -Csub test",
            "make --directory=sub deploy", "npm run build", "pnpm run dev", "yarn run lint",
            "bun run start", "dotnet build", "mvn package", "gradle build",
        )
        for command in defer:
            with self.subTest(command=command):
                # A positional word is a relative path too (`build`, `deploy`): its repo.write
                # finding can rank first, so only the decision is fixed here.
                self.assertEqual(self.decide(self.bash(command))["decision"], "allow")
        # With no positional word the form is only DEFER: the reason names the session mode.
        for command in ("make --version", "dotnet --version", "cargo --version"):
            with self.subTest(command=command):
                result = self.decide(self.bash(command))
                self.assertEqual((result["decision"], result["reason"]),
                                 ("allow", "policy.reason.defer"))
        # Known forms of the same toolchains keep their own capability.
        for command, capability in (("cargo test", "test.run"), ("make test", "test.run"),
                                    ("go vet ./...", "test.run"), ("npm test", "test.run")):
            with self.subTest(command=command):
                result = self.decide(self.bash(command))
                self.assertEqual((result["decision"], result["capability"]), ("allow", capability))
        # The path barrier applies to the arguments of a deferred form, inline values included.
        barrier = (
            ("cargo run --manifest-path=/etc/x", "repo.outside"),
            ("go run /etc/x.go", "repo.outside"),
            ("make -C /etc deploy", "repo.outside"),
            ("make -C/etc deploy", "repo.outside"),
            ("make --directory=/etc deploy", "repo.outside"),
            ("npm run build --prefix=.env", "secrets"),
        )
        for command, capability in barrier:
            with self.subTest(command=command):
                result = self.decide(self.bash(command))
                self.assertEqual((result["decision"], result["capability"]), ("deny", capability))
        # Arbitrary code forms defer (item 1xx); admin and secrets named in the line still deny, and
        # so does a known segment with a bad part.
        for command in ("sh -c 'cargo run'", "bash -c ls", "eval ls",
                        "python3 -c 'print(1)'", "go run ./x 'a b/c'"):
            with self.subTest(command=command):
                result = self.decide(self.bash(command))
                self.assertEqual((result["decision"], result["reason"]),
                                 ("allow", "policy.reason.defer"))
        for command, capabilities in (
                ("cargo run && cat .env", ("secrets",)), ("sudo cargo run", ("admin",)),
                ("sh -c 'sudo cargo run'", ("admin",)),
                # two denies in one line: the first one found is reported
                ("make -C /etc && sudo ls", ("repo.outside", "admin"))):
            with self.subTest(command=command):
                result = self.decide(self.bash(command))
                expected = "privileged" if capabilities == ("admin",) else "deny"
                self.assertEqual(result["decision"], expected)
                self.assertIn(result["capability"], capabilities)
        # `ask` mode (opt-in marker) asks about a deferred form instead of allowing it.
        self.ask_marker()
        for command in ("cargo run", "make", "npm run build"):
            with self.subTest(command=command, ask=True):
                result = self.decide(self.bash(command))
                self.assertEqual((result["decision"], result["reason"]),
                                 ("ask", "policy.reason.ask_mode"))

    def test_item_135_acceptance_battery(self):
        # Unknown programs and the install/net tools of a repo session pass; admin, secrets, the
        # config directory and the workspace code stay denied.
        cases = (
            (self.bash("dotnet --version"), "allow"),
            (self.bash("python3 -m venv .venv"), "allow"),
            (self.bash("pip install -e ."), "allow"),
            (self.bash("sudo ls"), "privileged"),
            (self.bash("pkexec ls"), "privileged"),
            (self.bash("cp a.txt ~/.config/ai-tmux-workspace/config.yaml"), "deny"),
            (self.write(ROOT / "src" / "policy.py"), "deny"),
            (self.bash("cat .env"), "deny"),
        )
        for event, decision in cases:
            with self.subTest(event=event):
                self.assertEqual(self.decide(event)["decision"], decision)

    def test_admin_stays_denied_for_every_role_and_mode(self):
        # sudo, pkexec, su and doas are the admin barrier: no mode and no role lifts them.
        for command in ("sudo ls", "pkexec ls", "su -c ls", "doas ls"):
            with self.subTest(command=command):
                result = self.decide(self.bash(command))
                self.assertEqual((result["decision"], result["capability"]), ("privileged", "admin"))
        with patched_env(WS_POLICY_AUTO="1"):
            self.assertEqual(self.decide(self.bash("sudo ls"))["decision"], "privileged")
        with patched_env(WS_SESSION_ROLE="home", WS_SESSION_ROOT=str(self.home)):
            self.assertEqual(self.decide(self.bash("pkexec ls"))["decision"], "privileged")
        self.ask_marker()
        self.assertEqual(self.decide(self.bash("doas ls"))["decision"], "privileged")

    def test_macos_admin_programs_are_admin_for_every_role(self):
        # The macOS admin tools deny like sudo in every role, in command position and after a
        # command wrapper or in a quoted script (the ambiguous path `_admin_in_text`).
        programs = (
            "sysadminctl", "dseditgroup", "dscl", "launchctl", "spctl", "csrutil", "fdesetup",
            "systemsetup", "networksetup", "diskutil", "security",
        )
        for program in programs:
            self.assertIn(program, policy.ADMIN_PROGRAMS)
        for role, root in (("repo", self.repo), ("home", self.home)):
            with patched_env(WS_SESSION_ROLE=role, WS_SESSION_ROOT=str(root)):
                for program in programs:
                    commands = (f"{program} --help", f"/usr/sbin/{program} --help",
                                f"/opt/homebrew/bin/{program} --help",
                                f"xargs {program} --help", f"sh -c '{program} --help'")
                    for command in commands:
                        with self.subTest(program=program, role=role, command=command):
                            result = policy.check_event(self.bash(command))
                            self.assertEqual((result["decision"], result["capability"]),
                                             ("privileged", "admin"))
                # A bare admin name used as an argument still defers (item 135), and so does the
                # rest of the unknown-program matrix: the new entries only change command position.
                for command in ("grep -r security src", "grep -r networksetup docs",
                                "grep -r diskutil .", "dotnet --version"):
                    with self.subTest(command=command, role=role):
                        result = policy.check_event(self.bash(command))
                        self.assertEqual((result["decision"], result["reason"]),
                                         ("allow", "policy.reason.defer"))
                self.assertEqual(policy.check_event(self.bash("cargo run"))["decision"], "allow")

    def test_homebrew_program_directories_are_recognized(self):
        for command in ("/opt/homebrew/bin/sudo ls", "/opt/homebrew/sbin/sudo ls",
                        "/opt/homebrew/bin/security find-generic-password",
                        "/opt/homebrew/bin/launchctl list",
                        "xargs /opt/homebrew/bin/sudo rm",
                        "sh -c '/opt/homebrew/sbin/sudo id'"):
            with self.subTest(command=command):
                result = self.decide(self.bash(command))
                self.assertEqual((result["decision"], result["capability"]), ("privileged", "admin"))
        # Another directory is not a program directory: the word is a path, not the program.
        result = self.decide(self.bash("sh -c '/usr/share/sudo id'"))
        self.assertEqual((result["decision"], result["reason"]),
                         ("allow", "policy.reason.defer"))

    def test_ask_mode_marker_turns_unknown_commands_and_tools_into_ask(self):
        self.ask_marker()
        events = (
            self.bash("some-unknown-command --flag"),
            self.obash("other-unknown-command"),
            {"tool_name": "UnknownTool", "tool_input": {}, "session_id": "sess-1"},
        )
        for event in events:
            with self.subTest(event=event):
                result = self.decide(event)
                self.assertEqual((result["decision"], result["capability"]), ("ask", None))
                self.assertEqual(result["reason"], "policy.reason.ask_mode")
        # The alias marker opens the same mode.
        (self.state / "policy.ask").unlink()
        self.ask_marker("policy.open")
        self.assertEqual(self.decide(self.bash("some-unknown-command"))["decision"], "ask")

    def test_ask_mode_env_var_opens_the_same_mode(self):
        for name, value in (("WS_POLICY_ASK", "1"), ("WS_POLICY_ASK", "true"),
                            ("WS_POLICY_OPEN", "yes")):
            with self.subTest(name=name, value=value):
                with patched_env(**{name: value}):
                    result = policy.check_event(self.bash("some-unknown-command"))
                    self.assertEqual((result["decision"], result["reason"]),
                                     ("ask", "policy.reason.ask_mode"))

    def test_ask_mode_marker_defaults_to_the_config_state_dir(self):
        marker = self.home / ".config" / "ai-tmux-workspace" / "state" / "policy.ask"
        marker.parent.mkdir(parents=True)
        marker.write_text("", encoding="utf-8")
        with patched_env(WS_STATE=None):
            self.assertEqual(policy._log_path(),
                             Path(self.home) / ".config" / "ai-tmux-workspace" / "state" / "policy.jsonl")
            self.assertEqual(policy.check_event(self.bash("some-unknown-command"))["decision"],
                             "ask")
        # With WS_STATE declared the config marker is not read: the test state is the only one.
        self.assertEqual(self.decide(self.bash("some-unknown-command"))["decision"], "allow")

    def test_ask_mode_keeps_secrets_admin_outside_config_and_workspace_denied(self):
        self.ask_marker()
        cases = (
            ("cat .env", "secrets"),
            ("ls ~/Desktop", "repo.outside"),
            ("git config --global user.name Someone", "config.write"),
        )
        for command, capability in cases:
            with self.subTest(command=command):
                result = self.decide(self.bash(command))
                self.assertEqual((result["decision"], result["capability"]), ("deny", capability))
                self.assertEqual(result["reason"], "policy.reason.deny")
        result = self.decide(self.bash("sudo ls"))
        self.assertEqual((result["decision"], result["capability"]), ("privileged", "admin"))
        for event in (self.write(ROOT / "src" / "policy.py"),
                      self.bash(f"echo x > {ROOT}/src/policy.py")):
            with self.subTest(event=event):
                result = self.decide(event)
                self.assertEqual((result["decision"], result["capability"]),
                                 ("deny", "workspace.write"))
        # The marker belongs to the person: a repo session cannot write it.
        result = self.decide(self.write(self.state / "policy.ask"))
        self.assertEqual((result["decision"], result["capability"]), ("deny", "repo.outside"))

    def test_ask_mode_leaves_repo_net_and_install_allowed_and_asks_the_rest(self):
        self.ask_marker()
        # The repo role allows net and install (item 135): ask mode only turns the denied ones into ask.
        cases = (
            ("curl https://example.com", "net"),
            ("pip install requests", "install"),
            ("python3 -m pip install requests", "install"),
        )
        for command, capability in cases:
            with self.subTest(command=command):
                result = self.decide(self.bash(command))
                self.assertEqual((result["decision"], result["capability"]),
                                 ("allow", capability))
        # Already `ask` in the table: the reason does not change.
        result = self.decide(self.bash("git pull"))
        self.assertEqual((result["decision"], result["reason"]), ("ask", "policy.reason.ask"))
        external = {"tool_name": "send_message", "tool_input": {}, "session_id": "sess-1"}
        self.assertEqual(self.decide(external)["reason"], "policy.reason.ask")
        with patched_env(WS_SESSION_ROLE="home", WS_SESSION_ROOT=str(self.home)):
            result = policy.check_event(self.bash("curl https://example.com"))
            self.assertEqual((result["decision"], result["reason"]),
                             ("ask", "policy.reason.ask"))

    def test_ask_mode_keeps_the_allows_and_the_dev_session_write(self):
        self.ask_marker()
        cases = (
            (self.bash("git status"), "vcs.read", "allow"),
            (self.bash("cat README.md"), "repo.read", "allow"),
            (self.write(self.repo / "a.py"), "repo.write", "allow"),
            (self.write("/tmp/policy-ask-mode.txt"), "tmp", "allow"),
            (self.read(ROOT / "src" / "policy.py"), "workspace.read", "allow"),
            (self.bash("python3 -m unittest discover -s tests"), "test.run", "allow"),
        )
        for event, capability, decision in cases:
            with self.subTest(event=event):
                result = self.decide(event)
                self.assertEqual((result["decision"], result["capability"]),
                                 (decision, capability))
        with patched_env(WS_DIR=str(self.repo), WS_SESSION_ROOT=str(self.repo)):
            result = policy.check_event(self.write(self.repo / "src" / "policy.py"))
            self.assertEqual((result["decision"], result["capability"]),
                             ("allow", "workspace.write"))

    def test_ask_mode_is_logged_with_the_tool_name_and_no_raw_command(self):
        self.ask_marker()
        secret = "SUPERSECRET-ASK-MODE"
        self.cli(self.bash(f"some-unknown-command --token {secret}"))
        self.cli(self.bash("curl https://example.com"))
        path = self.state / "policy.jsonl"
        text = path.read_text(encoding="utf-8")
        entries = [json.loads(line) for line in text.splitlines() if line]
        self.assertEqual(len(entries), 2)
        self.assertEqual((entries[0]["decision"], entries[0]["capability"]),
                         ("ask", None))
        self.assertEqual(entries[0]["reason"], "policy.reason.ask_mode")
        self.assertEqual(entries[0]["target"], "Bash")
        self.assertNotIn(secret, text)
        self.assertEqual((entries[1]["decision"], entries[1]["capability"]),
                         ("allow", "net"))
        self.assertEqual(entries[1]["target"], "curl")

    def test_cli_without_the_marker_defers_unknown_commands(self):
        result = self.cli(self.bash("some-unknown-command"))
        self.assertEqual(result, {"decision": "allow", "capability": None,
                                  "reason": "policy.reason.defer"})
        self.assertEqual(self.cli(self.bash("curl https://example.com"))["decision"], "allow")

    def test_cli_covers_the_item_119_acceptance_battery(self):
        self.ask_marker()
        cases = (
            (self.bash("some-unknown-command"), "ask", None),
            ({"tool_name": "UnknownTool", "tool_input": {}, "session_id": "s"},
             "ask", None),
            (self.bash("cat .env"), "deny", "secrets"),
            (self.bash("sudo ls"), "privileged", "admin"),
            (self.bash("ls ~/Desktop"), "deny", "repo.outside"),
            (self.write(ROOT / "src" / "policy.py"), "deny", "workspace.write"),
            (self.bash("curl https://example.com"), "allow", "net"),
            (self.bash("pip install requests"), "allow", "install"),
            (self.bash("git status"), "allow", "vcs.read"),
        )
        for event, decision, capability in cases:
            with self.subTest(event=event):
                result = self.cli(event)
                self.assertEqual((result["decision"], result["capability"]),
                                 (decision, capability))
        self.assertEqual(i18n.t("policy.reason.ask_mode"),
                         "Modo decisão: confirme esta ação.")

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

        # outside tmux nothing runs: the hook answers deny with the reason (item 154)
        deny = self.hook(self.bash("sudo ls"), TMUX=None)
        self.assertEqual(deny["hookSpecificOutput"]["permissionDecision"], "deny")
        self.assertEqual(deny["hookSpecificOutput"]["permissionDecisionReason"],
                         i18n.t("privileged.no_tmux", command="sudo ls"))

        self.assertIsNone(self.hook(self.bash("git status")))
        self.assertIsNone(self.hook({"tool_name": "TodoWrite", "tool_input": {}}))
        # An unknown tool name defers: the hook stays silent and the session's own flow decides.
        self.assertIsNone(self.hook({"tool_name": "UnknownTool", "tool_input": {}}))
        self.assertIsNone(self.hook(self.bash("grep -r x .")))

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
            (self.write(self.home / ".config" / "ai-tmux-workspace" / "config.yaml"),
             self.owrite(self.home / ".config" / "ai-tmux-workspace" / "config.yaml")),
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
        # the hook answers deny for a privileged command (the approval result), the engine says privileged
        plugin_decisions = [("deny" if entry["decision"] == "privileged" else entry["decision"])
                            for entry in json.loads(process.stdout)]
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
        self.assertEqual(result["deny"]["policy"]["decision"], "privileged")
        self.assertTrue(result["ask"]["error"])
        self.assertEqual(result["ask"]["policy"]["decision"], "ask")
        self.assertEqual(result["allow"]["decision"], "allow")

    # --- installation --------------------------------------------------------------------------
    def test_security_files_lists_the_policy_artifacts(self):
        names = installer.security_files(self.repo)
        for name in ("gate.py", "policy.yaml", "opencode-policy.js", "policy.py", "paths.py",
                     "yaml_min.py", "i18n/pt-BR.json", "settings.json"):
            self.assertIn(name, names)
        text = installer.harness_files(self.repo)
        for name in ("gate.py", "policy.yaml", "opencode-policy.js", "settings.json"):
            self.assertNotIn(name, text)

    def test_copy_harness_replaces_the_old_tree_and_recreates_the_mechanism(self):
        # Item 72: the installed harness of the old layout may still carry harness.py/policy.yaml/
        # settings.json inside harness/. The copy replaces the whole tree and recreates security/.
        target = self.root / "installed" / "harness"
        target.mkdir(parents=True)
        for name in ("harness.py", "policy.yaml", "settings.json"):
            (target / name).write_text("old\n", encoding="utf-8")
        source = self.root / "source-harness"
        source.mkdir()
        (source / "HARNESS.md").write_text("# Harness\n\nRead the rules.\n", encoding="utf-8")
        (source / "forcado.yaml").write_text("versao: 1\nitens: []\n", encoding="utf-8")
        installer.copy_harness(source, target)
        for name in ("harness.py", "policy.yaml", "settings.json"):
            self.assertFalse((target / name).exists(), f"old mechanism stayed in harness/: {name}")
        self.assertTrue((target / "HARNESS.md").is_file())
        security = target.parent / "security"
        for name in ("gate.py", "policy.yaml", "opencode-policy.js", "policy.py", "paths.py",
                     "settings.json"):
            self.assertTrue((security / name).is_file(), f"missing security file: {name}")

    def test_policy_installation_is_idempotent_and_self_contained(self):
        target = self.root / "installed"
        first = installer.install_policy_files(target)
        self.assertEqual(first, ["gate.py", "policy.yaml", "opencode-policy.js", "policy.py",
                                 "paths.py", "yaml_min.py", "i18n/pt-BR.json"])
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
