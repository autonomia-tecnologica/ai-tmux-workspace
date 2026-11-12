"""Portability guards for `bin/workspace`.

The launcher runs on the bash 3.2 that macOS still ships at /bin/bash, where `mapfile` (4.0) and
`${var@Q}` (4.4) do not exist, `readlink -f` only appears in macOS 12.3, and a repo under
"/Users/John Doe/..." contains spaces. These tests read the script, run its path/quoting helpers,
and drive `session-new`/`tab-step` against a fake tmux on PATH (same style as tests/test_lifecycle.py).
"""
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BIN = ROOT / "bin" / "workspace"
TEXT = BIN.read_text(encoding="utf-8")

# bash 4 features that would fail on the stock macOS shell (3.2), plus `readlink -f` (macOS < 12.3).
BASH4 = {
    "mapfile": re.compile(r"\bmapfile\b"),
    "${var@Q}": re.compile(r"\$\{[A-Za-z_][A-Za-z0-9_]*@Q\}"),
    "${var,,}": re.compile(r"\$\{[A-Za-z_][A-Za-z0-9_]*,"),
    "${var^^}": re.compile(r"\$\{[A-Za-z_][A-Za-z0-9_]*\^"),
    "local -n": re.compile(r"\blocal[ \t]+-n\b"),
    "wait -n": re.compile(r"\bwait[ \t]+-n\b"),
    "globstar": re.compile(r"\bshopt[ \t]+-s[ \t]+globstar\b"),
    "declare -A": re.compile(r"\bdeclare[ \t]+-A\b"),
    "readlink -f": re.compile(r"\breadlink[ \t]+-f\b"),
}

DEF_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)\(\) \{")

# Fake tmux: logs every call, and writes the last argument of `respawn-pane` (the shell command the
# pane will run) to $FAKE_COMMANDS, one per line, so the test can parse it with shlex.
FAKE_TMUX = """#!/usr/bin/env bash
printf '%s\\n' "$*" >> "$FAKE_LOG"
cmd="${1:-}"
key="${!#}"
last=""
for last in "$@"; do :; done
case "$cmd" in
  respawn-pane) printf '%s\\n' "$last" >> "$FAKE_COMMANDS" ;;
  show-environment) printf 'WS_PROFILE=claude\\n' ;;
  show)
    case "$key" in
      @ws_repo) printf '%s\\n' "$FAKE_REPO" ;;
      @ws_tabs) printf '%s\\n' "$FAKE_TABS" ;;
      @ws_active) printf '%s\\n' "$FAKE_ACTIVE" ;;
      @ws_next) printf '2\\n' ;;
      @ws_profile) printf 'claude\\n' ;;
      @ws_tab_id) printf 'tab-1\\n' ;;
      @ws_tab_profile) printf 'claude\\n' ;;
    esac ;;
  list-panes)
    case "$*" in
      *" -a "*) printf '%s\\n' "$FAKE_ALL_PANES" ;;
      *) printf '%s|claude\\n' "$FAKE_PANE" ;;
    esac ;;
  display|new-window) printf '%s\\n' "$FAKE_NEWPANE" ;;
esac
exit 0
"""


def functions(text):
    """name -> body of every top-level bash function (the file closes each with `}` at column 0)."""
    blocks, name, body = {}, None, []
    for line in text.splitlines():
        if name is None:
            match = DEF_RE.match(line)
            if not match:
                continue
            if line.count("{") <= line.count("}"):   # one-line function
                blocks[match.group(1)] = line
                continue
            name, body = match.group(1), [line]   # keep the header so the block can be executed
            continue
        if line == "}":
            blocks[name] = "\n".join(body + [line])
            name = None
        else:
            body.append(line)
    return blocks


def run_bash(script, *args, env=None):
    return subprocess.run(["bash", "-c", script, "bash", *args], env=env,
                          capture_output=True, text=True, timeout=60)


class SourceGuardTests(unittest.TestCase):
    """The launcher must not contain syntax the stock macOS bash cannot parse."""

    def test_no_bash4_syntax_and_no_readlink_f(self):
        for name, pattern in BASH4.items():
            with self.subTest(feature=name):
                found = [i for i, line in enumerate(TEXT.splitlines(), 1) if pattern.search(line)]
                self.assertEqual(found, [], f"{name} is not available on macOS bash 3.2")

    def test_script_parses(self):
        result = subprocess.run(["bash", "-n", str(BIN)], capture_output=True, text=True,
                                timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_the_helpers_replaced_the_bash4_features(self):
        found = functions(TEXT)
        self.assertIn("resolve_path", found)
        self.assertIn("quote", found)
        self.assertNotIn("mapfile", found["tab_step"])
        self.assertNotIn("@Q", found["session_new"])
        self.assertNotIn("@Q", found["tab_new"])


class ResolvePathTests(unittest.TestCase):
    """`resolve_path` canonicalises an existing parent and keeps a missing final component."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="path-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        # A PATH where `readlink -f` fails: the resolver may only use the plain calls of bash 3.2.
        self.bin = Path(self.tmp) / "bin"
        self.bin.mkdir()
        real = shutil.which("readlink")
        self.assertTrue(real, "readlink is required by the test")
        wrapper = self.bin / "readlink"
        wrapper.write_text(textwrap.dedent(f"""\
            #!/bin/sh
            for arg in "$@"; do
              if [ "$arg" = "-f" ]; then
                echo "readlink: illegal option -- f" >&2
                exit 1
              fi
            done
            exec {real} "$@"
            """), encoding="utf-8")
        wrapper.chmod(0o755)
        self.env = dict(os.environ, PATH=f"{self.bin}{os.pathsep}{os.environ.get('PATH', '')}")
        self.body = functions(TEXT)["resolve_path"]
        self.script = f"set -u\n{self.body}\nresolve_path \"$1\"\n"

    def resolve(self, path):
        result = run_bash(self.script, path, env=self.env)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout.rstrip("\n")

    def test_an_existing_directory_is_canonicalised(self):
        folder = Path(self.tmp) / "dir with space"
        folder.mkdir()
        self.assertEqual(self.resolve(str(folder)), os.path.realpath(folder))

    def test_a_missing_final_component_still_resolves(self):
        folder = Path(self.tmp) / "dir with space"
        folder.mkdir()
        missing = folder / "not-created"
        self.assertEqual(self.resolve(str(missing)), str(folder) + "/not-created")
        self.assertFalse(missing.exists())

    def test_symlinks_are_followed_without_readlink_f(self):
        folder = Path(self.tmp) / "real"
        folder.mkdir()
        link = Path(self.tmp) / "link"
        link.symlink_to(folder)
        self.assertEqual(self.resolve(str(link / "child")), str(folder) + "/child")


class LauncherOnMacOsLayoutTests(unittest.TestCase):
    """Run the real launcher through a symlink whose path has a space: the installed layout."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="mac-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        install = Path(self.tmp) / "John Doe" / ".local" / "bin"
        install.mkdir(parents=True)
        self.link = install / "workspace"
        self.link.symlink_to(BIN)
        self.home = Path(self.tmp) / "home"
        self.home.mkdir()
        self.env = {key: value for key, value in os.environ.items()
                    if key not in ("TMUX", "WS_PYTHON")}
        self.env.update(HOME=str(self.home), WS_STATE=str(Path(self.tmp) / "state"),
                        LANG="C.UTF-8")

    def test_help_resolves_the_real_script_through_the_install_symlink(self):
        result = subprocess.run(["bash", str(self.link), "--help"], env=self.env,
                                capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("install-hooks", result.stdout)
        self.assertNotIn("Traceback", result.stderr)


class TabCommandTests(unittest.TestCase):
    """`session-new` and `tab-step` reach the changed code without bash 4 syntax."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="tabs-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.root = Path(self.tmp)
        self.bin = self.root / "bin"
        self.bin.mkdir()
        tmux = self.bin / "tmux"
        tmux.write_text(FAKE_TMUX, encoding="utf-8")
        tmux.chmod(0o755)
        self.repo = self.root / "repo with space"
        self.repo.mkdir()
        self.home = self.root / "home"
        self.home.mkdir()
        self.log = self.root / "calls.log"
        self.commands = self.root / "commands.log"
        self.env = {
            "PATH": f"{self.bin}{os.pathsep}{os.environ.get('PATH', '')}",
            "HOME": str(self.home),
            "WS_STATE": str(self.root / "state"),
            "LANG": "C.UTF-8",
            "FAKE_LOG": str(self.log),
            "FAKE_COMMANDS": str(self.commands),
            "FAKE_REPO": str(self.repo),
            "FAKE_PANE": "%1",
            "FAKE_NEWPANE": "%9",
            "FAKE_ALL_PANES": "%1\n%9",
        }
        self.env.pop("TMUX", None)
        self.env.pop("WS_PYTHON", None)

    def run_workspace(self, *args, **overrides):
        env = dict(self.env, **overrides)
        return subprocess.run(["bash", str(BIN), *args], env=env, capture_output=True,
                              text=True, timeout=60)

    def calls(self):
        return self.log.read_text(encoding="utf-8").splitlines() if self.log.exists() else []

    def emitted(self):
        if not self.commands.exists():
            return []
        return self.commands.read_text(encoding="utf-8").splitlines()

    def test_session_new_quotes_a_repo_path_with_spaces(self):
        result = self.run_workspace("session-new", "%1", "claude",
                                    FAKE_TABS="%1", FAKE_ACTIVE="%1")
        self.assertEqual(result.returncode, 0, result.stderr)
        commands = self.emitted()
        self.assertTrue(commands, "session-new must respawn the pane with a command")
        argv = shlex.split(commands[-1])
        self.assertEqual(argv[:5], [str(BIN), "session-run", "%1", "claude", str(self.repo)])
        self.assertEqual(len(argv), 6, argv)              # the tab id is the last word, nothing split
        self.assertTrue(argv[5])

    def test_tab_step_switches_the_next_tab(self):
        result = self.run_workspace("tab-step", "%1", "1", "session",
                                    FAKE_TABS="%1 %9", FAKE_ACTIVE="%1")
        self.assertEqual(result.returncode, 0, result.stderr)
        swaps = [call for call in self.calls() if call.startswith("swap-pane")]
        self.assertEqual(len(swaps), 1, self.calls())
        self.assertIn("-s %9 -t %1", swaps[0])

    def test_tab_step_survives_a_single_tab(self):
        result = self.run_workspace("tab-step", "%1", "1", "session",
                                    FAKE_TABS="%1", FAKE_ACTIVE="%1")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual([c for c in self.calls() if c.startswith("swap-pane")], [])


class InterpreterQuotingTests(unittest.TestCase):
    """Every emitted command carries the interpreter as a single, quoted word."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="py-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.root = Path(self.tmp)
        folder = self.root / "John Doe" / "python bin"
        folder.mkdir(parents=True)
        self.python = folder / "python3"
        self.python.symlink_to(sys.executable)
        self.home = self.root / "home"
        self.home.mkdir()

    def env(self):
        env = {key: value for key, value in os.environ.items()
               if key not in ("TMUX", "WS_PYTHON")}
        env.update(HOME=str(self.home), WS_STATE=str(self.root / "state"), LANG="C.UTF-8",
                   WS_PYTHON=str(self.python))
        return env

    def test_install_hooks_quotes_the_interpreter_and_the_hook_path(self):
        result = subprocess.run(["bash", str(BIN), "install-hooks"], env=self.env(),
                                capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)
        settings = Path(self.home) / ".claude" / "settings.json"
        cfg = json.loads(settings.read_text(encoding="utf-8"))
        commands = [hook["command"]
                    for groups in cfg["hooks"].values() for group in groups
                    for hook in group["hooks"]]
        commands.append(cfg["statusLine"]["command"])
        self.assertTrue(commands)
        for command in commands:
            with self.subTest(command=command):
                self.assertFalse(command.startswith("python3 "), command)
                argv = shlex.split(command)
                self.assertEqual(argv[0], str(self.python))
                self.assertEqual(len(argv), 2, argv)         # interpreter + hook path, no split words

    def test_loop_cmd_quotes_the_interpreter_and_the_panel_path(self):
        found = functions(TEXT)
        script = "\n".join([
            "set -u",
            f"PYTHON={shlex.quote(str(self.python))}",
            f"SRC={shlex.quote(str(self.root / 'src with space'))}",
            found["quote"],
            found["loop_cmd"],
            'loop_cmd repos claude',
        ]) + "\n"
        result = run_bash(script, env=self.env())
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("while :; do ", result.stdout)
        inner = result.stdout.split("while :; do ", 1)[1].split("; sleep", 1)[0]
        self.assertEqual(shlex.split(inner),
                         [str(self.python), str(self.root / "src with space" / "panel_repos.py"),
                          "claude"])

    def test_no_bare_python3_command_is_written_into_the_hooks(self):
        # Static guard: the generated settings must quote the interpreter with shlex.quote.
        block = functions(TEXT)["install_hooks"]
        self.assertIn("shlex.quote", block)
        self.assertNotIn('f"python3 {', block)   # the old bare-interpreter template


if __name__ == "__main__":
    unittest.main()
