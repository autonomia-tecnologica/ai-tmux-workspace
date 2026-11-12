"""Guard: terminal drawing has a single owner (`src/viewer.py`).

A panel returns `collect(viewer) -> (header, body)` and never paints. The effects reserved for the
owner are the full-screen clear (`\\x1b[2J`), cursor home (`\\x1b[H`), alt-screen (`?1049h`), a
module-level raw-terminal import (`termios`/`tty`) and a SIGWINCH handler that draws.

`sys.stdout.write` is deliberately not guarded: `policy.py` (hook JSON) and `session_risk.py` (the
confirmation prompt) write to stdout without painting a screen. For the same reason, a function-local
raw import is a one-shot key read (`session_risk._read_key`, `conference._read_key`), not a module
owning the terminal for its lifetime, so only module-level imports are guarded.

`ALLOWED` is today's debt. It may only shrink: every entry carries its reason, and the exact set is
pinned by `EXPECTED_ALLOWED`, so adding a new exception needs an explicit edit to this test (and
removing one means editing both down).

The same file also guards the architecture document: `ARCH.md` must exist at the root, keep every
minimum section of `harness/arch-format.md`, and name every top-level `src/*.py` in one of its
layers. `src/agents/` enters by layer, not by this test.

The layer rule is read from the `## Camadas` table of `ARCH.md`: a module-level import may only go to
the same layer or a lower one (fundação, sessão, estado, desenho, entrada, bottom to top). Function-local
imports are the declared way to break a cycle and are not checked here. The tmux command has one owner
(`src/tmux.py`): no other module runs it through `subprocess` or `os`. `IMPORT_ALLOWED` and
`TMUX_ALLOWED` are today's debt and only shrink; a stale entry fails the test.
"""
import ast
import re
import unittest
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
ARCH = ROOT / "ARCH.md"

# Exact titles required by harness/arch-format.md.
ARCH_SECTIONS = (
    "## Camadas",
    "## Direção de dependência",
    "## Dono único por efeito",
    "## Eventos",
    "## Estado vivo no tmux",
    "## UI",
    "## Ciclo de vida de sessão",
    "## Adaptadores de agente",
    "## Config e estado",
    "## Idioma e contratos",
    "## Dívidas conhecidas",
    "## Manutenção",
)

# Modules allowed to emit a guarded terminal effect, each with the reason it is still an exception.
ALLOWED = {
    "viewer.py": "owner: the only module that may paint rows, use alt-screen and redraw on SIGWINCH",
    "panel_tabs.py": "debt: paints the tab strip (full clear + cbreak) in its own loop",
    "session_placeholder.py": ("debt, legitimate: draws alone (clear + SIGWINCH) so it survives a "
                               "reload without lib/tmux"),
    "mic.py": "debt: owns the dictation full-screen loop (clear + alt-screen) outside viewer",
    "conference.py": ("debt: reads one key in raw mode for the request popup; the conference "
                      "screen itself draws through viewer"),
}

EXPECTED_ALLOWED = frozenset({
    "viewer.py",
    "panel_tabs.py",
    "session_placeholder.py",
    "mic.py",
    "conference.py",
})

# Escape literals as they appear in the source text (`\\x1b` is the two characters backslash-x1b).
CLEAR_SCREEN = re.compile(r"\\x1b\[2J")
CURSOR_HOME = re.compile(r"\\x1b\[H")
ALTERNATE_SCREEN = re.compile(r"\\x1b\[\?1049h")

# A module-level import owns raw mode for the module's lifetime. Function-local imports are one-shot
# key reads and are intentionally out of this guard (see the module docstring).
RAW_IMPORT = re.compile(r"^(?:import[ \t]+(?:termios|tty)\b|from[ \t]+(?:termios|tty)[ \t]+import\b)",
                        re.M)

SIGWINCH = re.compile(r"^[ \t]*signal\.signal\(\s*signal\.SIGWINCH\s*,\s*(.+?)\)[ \t]*$", re.M)
DRAW_CALL = re.compile(r"\b(?:draw|paint|redraw|render|_draw|_paint|_redraw)\s*\(")
STDOUT_WRITE = re.compile(r"\bsys\.stdout\.write\s*\(")


def _function_body(text, name):
    """Body of the top-level `def name(...)`, or None when it is not defined in this file."""
    lines = text.splitlines()
    header = re.compile(rf"^(\s*)def\s+{re.escape(name)}\s*\(")
    for i, line in enumerate(lines):
        match = header.match(line)
        if not match:
            continue
        indent = len(match.group(1))
        body = []
        for following in lines[i + 1:]:
            if following.strip() and len(following) - len(following.lstrip()) <= indent:
                break
            body.append(following)
        return "\n".join(body)
    return None


def handler_redraws(text, handler):
    """True when the SIGWINCH `handler` paints directly instead of only waking the owner loop."""
    handler = handler.strip()
    if handler.startswith("lambda"):
        body = handler.split(":", 1)[1] if ":" in handler else ""
    else:
        body = _function_body(text, handler)
        if body is None:      # cannot prove that it is not a redraw: fail closed
            return True
    return bool(DRAW_CALL.search(body) or STDOUT_WRITE.search(body))


def effects(text):
    """Names of the guarded terminal effects found in `text`."""
    found = []
    if CLEAR_SCREEN.search(text):
        found.append("full-screen clear (\\x1b[2J)")
    if CURSOR_HOME.search(text):
        found.append("cursor home (\\x1b[H)")
    if ALTERNATE_SCREEN.search(text):
        found.append("alt-screen (\\x1b[?1049h)")
    if RAW_IMPORT.search(text):
        found.append("module-level raw terminal import")
    if any(handler_redraws(text, match.group(1)) for match in SIGWINCH.finditer(text)):
        found.append("SIGWINCH handler that redraws")
    return found


def offenders():
    """module name -> effects it emits, for every module outside the allowlist."""
    found = {}
    for path in sorted(SRC.glob("*.py")):
        if path.name in ALLOWED:
            continue
        emitted = effects(path.read_text(encoding="utf-8"))
        if emitted:
            found[path.name] = emitted
    return found


class ArchitectureTest(unittest.TestCase):
    def test_only_allowed_modules_emit_terminal_effects(self):
        bad = offenders()
        self.assertEqual(
            bad, {},
            "terminal drawing belongs to viewer.py; migrate the module to collect(viewer) "
            "instead of adding it to ALLOWED: " + repr(bad),
        )

    def test_allowlist_is_exactly_the_known_debt(self):
        self.assertEqual(set(ALLOWED), EXPECTED_ALLOWED)

    def test_every_exception_has_a_reason(self):
        for name, reason in ALLOWED.items():
            with self.subTest(module=name):
                self.assertTrue(reason.strip(), f"{name} needs a reason")

    def test_session_placeholder_exception_is_documented(self):
        path = SRC / "session_placeholder.py"
        doc = ast.get_docstring(ast.parse(path.read_text(encoding="utf-8"))) or ""
        self.assertIn("reload", doc)
        self.assertIn("lib/tmux", doc)
        self.assertIn("tests/test_architecture.py", doc)

    def test_viewer_docstring_has_the_effect_owner_table(self):
        doc = ast.get_docstring(ast.parse((SRC / "viewer.py").read_text(encoding="utf-8"))) or ""
        for effect in ("drawing rows", "full-screen clear", "alt-screen", "SIGWINCH redraw",
                       "cursor / mouse", "raw input"):
            with self.subTest(effect=effect):
                self.assertIn(effect, doc)
        self.assertIn("collect(viewer)", doc)

    def test_full_screen_clear_is_an_effect(self):
        self.assertTrue(effects(r'print("\x1b[2J")'))
        self.assertTrue(effects(r'out = "\x1b[H"'))
        self.assertTrue(effects(r'out = "\x1b[?1049h"'))

    def test_sigwinch_handler_that_redraws_is_an_effect(self):
        text = "def redraw(*_):\n    draw()\n\nsignal.signal(signal.SIGWINCH, redraw)\n"
        self.assertIn("SIGWINCH handler that redraws", effects(text))

    def test_sigwinch_handler_that_only_wakes_is_not_an_effect(self):
        text = "signal.signal(signal.SIGWINCH, lambda *_: self._wake())\n"
        self.assertNotIn("SIGWINCH handler that redraws", effects(text))

    def test_stdout_write_is_not_an_effect(self):
        self.assertEqual(effects('sys.stdout.write("{\\"ok\\": true}")\n'), [])


def arch_headings(text):
    """Level-2 headings of `text`, stripped, so a section title is matched as a heading."""
    return {line.strip() for line in text.splitlines() if line.startswith("## ")}


class ArchDocumentTest(unittest.TestCase):
    def test_arch_md_exists(self):
        self.assertTrue(
            ARCH.is_file(),
            "ARCH.md is missing at the repo root: it declares the layers, the dependency direction "
            "and the single owner per effect (see harness/arch-format.md)",
        )

    def test_arch_md_has_every_minimum_section(self):
        headings = arch_headings(ARCH.read_text(encoding="utf-8"))
        missing = [section for section in ARCH_SECTIONS if section not in headings]
        self.assertEqual(
            missing, [],
            "ARCH.md is missing the minimum section(s): " + ", ".join(missing),
        )

    def test_every_src_module_is_named_in_arch_md(self):
        text = ARCH.read_text(encoding="utf-8")
        missing = [path.name for path in sorted(SRC.glob("*.py"))
                   if f"src/{path.name}" not in text]
        self.assertEqual(
            missing, [],
            "ARCH.md must name every top-level src/*.py in a layer table; missing: "
            + ", ".join(missing) + " (src/agents/ enters by layer, not by this test)",
        )


# Module-level imports that go up a layer today, each with the debt that keeps it (ARCH.md,
# "Dívidas conhecidas"). Only shrinks: a fixed debt must leave this dict in the same commit.
IMPORT_ALLOWED = {
    ("agents/claude.py", "conference.py"):
        "debt: estado imports desenho at module level for the conference constants",
    ("agents/opencode.py", "conference.py"):
        "debt: estado imports desenho at module level for the conference constants",
}
EXPECTED_IMPORT_ALLOWED = frozenset({
    ("agents/claude.py", "conference.py"),
    ("agents/opencode.py", "conference.py"),
})

# Modules allowed to run the tmux command: the owner only (ARCH.md, "Dono único por efeito").
TMUX_ALLOWED = {
    "tmux.py": "owner: the only module that runs the tmux command",
}
EXPECTED_TMUX_ALLOWED = frozenset({"tmux.py"})

SUBPROCESS_CALLS = frozenset({"run", "Popen", "call", "check_call", "check_output",
                              "getoutput", "getstatusoutput"})
TMUX_COMMAND = re.compile(r"tmux(\s|$)")


def layer_rows(arch_text):
    """(layer, paths) per row of the `## Camadas` table, in table order (bottom to top)."""
    section = arch_text.split("## Camadas", 1)[1].split("\n## ", 1)[0]
    rows = []
    for line in section.splitlines():
        if not line.startswith("|") or set(line) <= set("|- "):
            continue
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if len(cells) < 2 or cells[0] == "camada":
            continue
        paths = [path for token in re.findall(r"`([^`]+)`", cells[1])
                 for path in re.split(r",\s*", token)]
        rows.append((cells[0], paths))
    return rows


def src_ranks(arch_text):
    """{module: (rank, layer)} for each src module or folder the table names; rank 0 is the bottom.

    Rows that name no src path (`harness/`, `tests/`, `i18n/`) take no rank.
    """
    ranks = {}
    rank = 0
    for layer, paths in layer_rows(arch_text):
        modules = [path[len("src/"):] for path in paths if path.startswith("src/")]
        if not modules:
            continue
        for module in modules:
            ranks[module] = (rank, layer)
        rank += 1
    return ranks


def layer_for(module, ranks):
    """(rank, layer) of a src module (`lib.py`, `agents/base.py`), or None when the table omits it."""
    if module in ranks:
        return ranks[module]
    for key, value in ranks.items():
        if key.endswith("/") and module.startswith(key):
            return value
    return None


def known_src_modules():
    """Every src module path relative to src/, `agents/__init__.py` included."""
    return frozenset(path.relative_to(SRC).as_posix() for path in SRC.rglob("*.py"))


def _module_level_nodes(tree):
    """AST nodes that run at import time: everything except the bodies of functions."""
    stack = list(tree.body)
    while stack:
        node = stack.pop()
        yield node
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            stack.extend(ast.iter_child_nodes(node))


def _resolve(parts, known):
    """The src module a dotted name points to, or None (stdlib and third-party names)."""
    for size in range(len(parts), 0, -1):
        stem = "/".join(parts[:size])
        for candidate in (stem + ".py", stem + "/__init__.py"):
            if candidate in known:
                return candidate
    return None


def imported_modules(text, importer, known):
    """src modules imported at module level by `text`, which lives at `importer` (src-relative)."""
    found = set()
    for node in _module_level_nodes(ast.parse(text)):
        if isinstance(node, ast.Import):
            targets = [alias.name.split(".") for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            head = []
            if node.level:
                head = list(PurePosixPath(importer).parent.parts)
                head = head[:max(0, len(head) - (node.level - 1))]
            if node.module is None:
                targets = [head + [alias.name] for alias in node.names]
            else:
                targets = [head + node.module.split(".")]
        else:
            continue
        for parts in targets:
            resolved = _resolve(parts, known)
            if resolved:
                found.add(resolved)
    return found


def upward_imports(text, importer, ranks, known):
    """Modules that `importer` imports at module level from a higher layer."""
    own = layer_for(importer, ranks)
    found = []
    for target in sorted(imported_modules(text, importer, known)):
        theirs = layer_for(target, ranks)
        if own and theirs and theirs[0] > own[0]:
            found.append(target)
    return found


def tmux_calls(text):
    """Line numbers of subprocess/os calls whose argv runs the `tmux` command.

    Follows `import subprocess as sp` and `from subprocess import run` to the real module name.
    """
    tree = ast.parse(text)
    modules = {"subprocess": "subprocess", "os": "os"}   # local name -> module it stands for
    direct = {}                                          # local name -> (module, function)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name in ("subprocess", "os"):
                    modules[alias.asname or alias.name] = alias.name
        elif isinstance(node, ast.ImportFrom) and node.module in ("subprocess", "os"):
            for alias in node.names:
                direct[alias.asname or alias.name] = (node.module, alias.name)
    lines = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
            owner, name = modules.get(func.value.id), func.attr
        elif isinstance(func, ast.Name) and func.id in direct:
            owner, name = direct[func.id]
        else:
            continue
        runs_process = ((owner == "subprocess" and name in SUBPROCESS_CALLS)
                        or (owner == "os" and name in {"system", "popen"}))
        if not runs_process:
            continue
        if any(isinstance(item, ast.Constant) and isinstance(item.value, str)
               and TMUX_COMMAND.match(item.value) for item in ast.walk(node)):
            lines.append(node.lineno)
    return lines


class LayerRuleTest(unittest.TestCase):
    def setUp(self):
        self.ranks = src_ranks(ARCH.read_text(encoding="utf-8"))
        self.known = known_src_modules()

    def test_every_src_module_has_a_layer(self):
        missing = [module for module in sorted(self.known) if layer_for(module, self.ranks) is None]
        self.assertEqual(
            missing, [],
            "ARCH.md Camadas table must place every src module in a layer: " + ", ".join(missing),
        )

    def test_module_level_imports_never_go_up_a_layer(self):
        bad = {}
        for module in sorted(self.known):
            text = (SRC / module).read_text(encoding="utf-8")
            for target in upward_imports(text, module, self.ranks, self.known):
                if (module, target) not in IMPORT_ALLOWED:
                    bad[f"{module} -> {target}"] = (f"{layer_for(module, self.ranks)[1]} -> "
                                                    f"{layer_for(target, self.ranks)[1]}")
        self.assertEqual(
            bad, {},
            "a lower layer must not import a higher one (ARCH.md, Direção de dependência); "
            "break the cycle with a function-local import and a reason: " + repr(bad),
        )

    def test_import_allowlist_is_exactly_the_known_debt(self):
        self.assertEqual(set(IMPORT_ALLOWED), EXPECTED_IMPORT_ALLOWED)

    def test_import_allowlist_entries_still_violate(self):
        # A fixed debt must leave IMPORT_ALLOWED (the allowlist only shrinks).
        for module, target in sorted(IMPORT_ALLOWED):
            with self.subTest(module=module, target=target):
                text = (SRC / module).read_text(encoding="utf-8")
                self.assertIn(target, upward_imports(text, module, self.ranks, self.known))

    def test_upward_import_is_reproved(self):
        # Planted: foundation (`i18n.py`) importing a higher layer must fail.
        found = upward_imports("import lib\n", "i18n.py", self.ranks, self.known)
        self.assertEqual(found, ["lib.py"])

    def test_upward_from_import_is_reproved(self):
        found = upward_imports("from viewer import draw\n", "lib.py", self.ranks, self.known)
        self.assertEqual(found, ["viewer.py"])

    def test_downward_and_same_layer_imports_pass(self):
        self.assertEqual(upward_imports("import lib\n", "viewer.py", self.ranks, self.known), [])
        self.assertEqual(upward_imports("import viewer\n", "panel_tabs.py", self.ranks, self.known), [])

    def test_function_local_import_is_not_module_level(self):
        text = "def load():\n    import viewer\n"
        self.assertEqual(upward_imports(text, "lib.py", self.ranks, self.known), [])


class TmuxOwnerTest(unittest.TestCase):
    def setUp(self):
        self.known = known_src_modules()

    def test_only_the_tmux_owner_runs_the_tmux_command(self):
        bad = {}
        for module in sorted(self.known):
            if module in TMUX_ALLOWED:
                continue
            lines = tmux_calls((SRC / module).read_text(encoding="utf-8"))
            if lines:
                bad[module] = lines
        self.assertEqual(
            bad, {},
            "only src/tmux.py runs `tmux` (ARCH.md, Dono único por efeito); call tmux.run or a "
            "tmux.py function instead: " + repr(bad),
        )

    def test_tmux_allowlist_is_exactly_the_owner(self):
        self.assertEqual(set(TMUX_ALLOWED), EXPECTED_TMUX_ALLOWED)

    def test_tmux_allowlist_entries_still_run_tmux(self):
        for module in sorted(TMUX_ALLOWED):
            with self.subTest(module=module):
                self.assertTrue(tmux_calls((SRC / module).read_text(encoding="utf-8")))

    def test_subprocess_running_tmux_is_an_effect(self):
        self.assertEqual(tmux_calls('subprocess.run(["tmux", "ls"], capture_output=True)\n'), [1])
        self.assertEqual(tmux_calls('os.system("tmux kill-server")\n'), [1])

    def test_aliased_subprocess_running_tmux_is_an_effect(self):
        self.assertEqual(tmux_calls('import subprocess as sp\nsp.run(["tmux", "ls"])\n'), [2])
        self.assertEqual(tmux_calls('from subprocess import run\nrun(["tmux", "ls"])\n'), [2])

    def test_subprocess_running_other_commands_is_not_an_effect(self):
        self.assertEqual(tmux_calls('subprocess.run(["git", "status"])\n'), [])


if __name__ == "__main__":
    unittest.main()
