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
"""
import ast
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
ARCH = ROOT / "ARCH.md"

# Exact titles required by harness/arch-format.md.
ARCH_SECTIONS = (
    "## Camadas",
    "## Direção de dependência",
    "## Dono único por efeito",
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


if __name__ == "__main__":
    unittest.main()
