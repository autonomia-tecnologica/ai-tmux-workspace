"""Full-screen installer wizard for `install.sh` ("next, next, finish"): screens, keys and drawing.

Stdlib only (curses). Three layers in one file, kept apart on purpose:

- screen logic (`Plan`, `Wizard`, `Progress`, `summary_lines`, `view`): plain data and key handling,
  no terminal, tested directly;
- layout (`layout`): turns a view into rows for a terminal size, also without curses;
- drawing (`draw`, `key_name`, `run`): the only code that talks to curses, and only when `can_run`
  says a terminal can take it.

This module never imports `installer` (that would go up a layer, ARCH.md): the caller passes a `Plan`
with the texts and paths it computed, and a `perform(answers, progress)` function that installs. The
answers are collected on the screens and nothing is written until the summary's "Instalar". Every
end-user text comes from i18n/pt-BR.json (`installer.tui.*`, plus the `installer.deps.*` lines).
"""
import copy
import os
import textwrap
from dataclasses import dataclass

from i18n import t

WELCOME, DEPS, MANAGE, HARNESS, PROVIDERS, DEFAULT, SUMMARY, PROGRESS, DONE = (
    "welcome", "deps", "manage", "harness", "providers", "default", "summary", "progress", "done")
HELP_KEYS = {MANAGE: "installer.tui.manage.help", HARNESS: "installer.tui.harness.help",
             PROVIDERS: "installer.tui.providers.help", DEFAULT: "installer.tui.default.help"}

# What handle_key returns for the drawing loop to act on.
QUIT, INSTALL, CLOSE = "quit", "install", "close"
CANCELLED = 1           # exit code of a cancelled wizard: the same as the text mode's abort


@dataclass
class Plan:
    """What the wizard shows before anything is written: texts and paths computed by the installer."""
    providers: list
    dependency_lines: list
    cli_link: str
    config_path: str
    harness_path: str
    settings_path: str
    shim_path: str
    clone_url: str
    harness_missing: bool = False
    legacy_config: str = ""     # display path of an old config that the install leaves alone


class Progress:
    """What the install printed, one step per section. `listener` runs after every change (a redraw)."""

    def __init__(self):
        self.steps = []         # [title, [lines]]
        self.code = None        # exit code once the install has returned
        self.listener = None

    def add_section(self, title):
        self.steps.append([title, []])
        self._changed()

    def add_line(self, text):
        if not self.steps:
            self.steps.append(["", []])
        self.steps[-1][1].extend(str(text).split("\n"))
        self._changed()

    def finish(self, code):
        self.code = code
        self._changed()

    def _changed(self):
        if self.listener is not None:
            self.listener()

    def rows(self):
        rows = []
        for title, lines in self.steps:
            if title:
                rows.append(title)
            rows.extend("   " + line for line in lines)
        return rows


class Wizard:
    """The answers and the current screen. Key names come from `key_name`; see `handle_key`."""

    def __init__(self, plan):
        self.plan = plan
        self.answers = {"manage": True, "harness_managed": True, "providers": [], "default": None}
        self.screen = WELCOME
        self.cursor = 0

    def screens(self):
        """The screens in order, for the answers so far: the harness question always (its order is the
        one the person asked for, whatever `manage` says), the default profile only when something is
        marked, and no question at all when an old config is left alone."""
        if self.plan.legacy_config:
            return [WELCOME, DEPS, SUMMARY, PROGRESS, DONE]
        names = [WELCOME, DEPS, MANAGE, HARNESS, PROVIDERS]
        if self.answers["providers"]:
            names.append(DEFAULT)
        return names + [SUMMARY, PROGRESS, DONE]

    def options(self):
        """(value, label) rows of the current screen's list; empty when the screen has none."""
        if self.screen in (MANAGE, HARNESS):
            return [(True, t("installer.tui.yes")), (False, t("installer.tui.no"))]
        if self.screen == PROVIDERS:
            return [(name, name) for name in self.plan.providers]
        if self.screen == DEFAULT:
            return [(name, name) for name in self.answers["providers"]]
        return []

    def is_marked(self, screen, value):
        if screen == MANAGE:
            return value == self.answers["manage"]
        if screen == HARNESS:
            return value == self.answers["harness_managed"]
        if screen == PROVIDERS:
            return value in self.answers["providers"]
        if screen == DEFAULT:
            return value == self.answers["default"]
        return False

    def goto(self, screen):
        self.screen = screen
        self.cursor = 0
        for index, (value, _label) in enumerate(self.options()):
            if self.is_marked(screen, value):
                self.cursor = index
                break

    def next(self):
        """Advances one screen. On the summary it returns INSTALL: the caller runs the install."""
        if self.screen == SUMMARY:
            return INSTALL
        names = self.screens()
        self.goto(names[names.index(self.screen) + 1])
        return None

    def back(self):
        names = self.screens()
        position = names.index(self.screen)
        if self.screen in (WELCOME, PROGRESS, DONE) or position == 0:
            return
        self.goto(names[position - 1])

    def handle_key(self, key):
        """Applies one key name (`up`, `down`, `space`, `enter`, `left`, `backspace`, `b`, `q`).

        Returns QUIT (cancel before any install), INSTALL (summary confirmed), CLOSE (the final screen
        closed) or None. Nothing is installed here: the caller acts on the result.
        """
        if self.screen == PROGRESS:
            return None
        if self.screen == DONE:
            return CLOSE if key in ("enter", "q") else None
        if key == "q":          # Esc is not a cancel: an arrow key split by the terminal must not quit
            return QUIT
        options = self.options()
        if key == "up":
            self.cursor = max(0, self.cursor - 1)
        elif key == "down":
            self.cursor = min(max(0, len(options) - 1), self.cursor + 1)
        elif key == "space" and options:
            self._mark(options[self.cursor][0])
        elif key in ("left", "b", "backspace"):
            self.back()
        elif key == "enter":
            if options and self.screen != PROVIDERS:
                self._mark(options[self.cursor][0])
            return self.next()
        return None

    def _mark(self, value):
        if self.screen == MANAGE:
            self.answers["manage"] = value
        elif self.screen == HARNESS:
            self.answers["harness_managed"] = value
        elif self.screen == PROVIDERS:
            chosen = set(self.answers["providers"]) ^ {value}
            self.answers["providers"] = [name for name in self.plan.providers if name in chosen]
        elif self.screen == DEFAULT:
            self.answers["default"] = value
        self._normalize()

    def _normalize(self):
        """Keeps the answers consistent: the default is one of the marked providers."""
        chosen = self.answers["providers"]
        if not chosen:
            self.answers["default"] = None
        elif self.answers["default"] not in chosen:
            self.answers["default"] = chosen[0]


def _yes_no(value):
    return t("installer.tui.yes") if value else t("installer.tui.no")


def summary_lines(wizard):
    """What the install will create or change, for the answers given (the summary screen)."""
    plan, answers = wizard.plan, wizard.answers
    manage, harness = answers["manage"], answers["harness_managed"]
    lines = [t("installer.tui.summary.cli", link=plan.cli_link), t("installer.tui.summary.completion")]
    if plan.legacy_config:
        lines.append(t("installer.tui.summary.config_legacy", path=plan.legacy_config))
    else:
        lines.append(t("installer.tui.summary.config", path=plan.config_path, manage=_yes_no(manage),
                       providers=", ".join(answers["providers"]) or t("installer.tui.none"),
                       default=answers["default"] or t("installer.tui.none")))
    if harness:
        if plan.harness_missing:
            lines.append(t("installer.tui.summary.clone", url=plan.clone_url))
        lines.append(t("installer.tui.summary.harness", path=plan.harness_path))
        if not manage:
            lines.append(t("installer.tui.summary.shim", path=plan.shim_path))
    else:
        lines.append(t("installer.tui.summary.nothing_harness"))
    if manage:
        lines.append(t("installer.tui.summary.hooks", path=plan.settings_path))
    else:
        lines.append(t("installer.tui.summary.nothing_hooks"))
    lines.append(t("installer.tui.summary.manifest"))
    return lines


def view(wizard, progress):
    """The current screen as data: title, text lines, option rows (label, mark, focused), hint, buttons.

    `tail` keeps the last rows when the screen is too short (the install log)."""
    screen, plan = wizard.screen, wizard.plan
    # The final screen's title depends on the result (done_ok / done_failed), set in its branch below.
    result = {"title": "" if screen == DONE else t("installer.tui.title." + screen), "lines": [],
              "options": [], "hint": "", "buttons": [], "tail": False}
    back, nxt = t("installer.tui.btn.back"), t("installer.tui.btn.next")
    if screen == DONE:
        ok = progress.code == 0
        result.update(title=t("installer.tui.title.done_ok" if ok else "installer.tui.title.done_failed"),
                      lines=progress.rows(), tail=True, buttons=[t("installer.tui.btn.finish")],
                      hint=t("installer.tui.hint.done" if ok else "installer.tui.hint.failed"))
    elif screen == PROGRESS:
        result.update(lines=progress.rows(), tail=True, hint=t("installer.tui.hint.progress"))
    elif screen == WELCOME:
        result.update(lines=t("installer.tui.welcome.text").split("\n"),
                      buttons=[t("installer.tui.btn.cancel"), nxt], hint=t("installer.tui.hint.nav"))
    elif screen == DEPS:
        result.update(lines=list(plan.dependency_lines), buttons=[back, nxt],
                      hint=t("installer.tui.hint.nav"))
    elif screen == SUMMARY:
        result.update(lines=summary_lines(wizard), buttons=[back, t("installer.tui.btn.install")],
                      hint=t("installer.tui.hint.nav"))
    else:
        radio = screen != PROVIDERS
        result.update(lines=[t(HELP_KEYS[screen])], buttons=[back, nxt],
                      hint=t("installer.tui.hint.radio" if radio else "installer.tui.hint.providers"))
        for index, (value, label) in enumerate(wizard.options()):
            marked = wizard.is_marked(screen, value)
            if radio:
                mark = "(*)" if marked else "( )"
            else:
                mark = "[x]" if marked else "[ ]"
            result["options"].append((label, mark, index == wizard.cursor))
    return result


def layout(view_data, width, height):
    """Rows of a view as (text, style) for a terminal of `width` x `height`. Styles: title, focus, hint,
    button, normal. Too short a terminal keeps the footer and clips the body (`tail` keeps its end)."""
    width, height = max(10, width), max(0, height)
    body = [(view_data["title"], "title"), ("", "normal")]
    for line in view_data["lines"]:
        body.extend((row, "normal") for row in (textwrap.wrap(line, width - 2) or [""]))
    if view_data["options"]:
        body.append(("", "normal"))
    for label, mark, focused in view_data["options"]:
        prefix = ">" if focused else " "
        body.append((f"{prefix} {mark} {label}", "focus" if focused else "normal"))
    footer = [("", "normal"), (view_data["hint"], "hint")]
    if view_data["buttons"]:
        footer.append(("   ".join(f"[ {label} ]" for label in view_data["buttons"]), "button"))
    room = max(0, height - len(footer))
    if room == 0:
        rows = []
    elif view_data["tail"]:
        rows = body[-room:]
    else:
        rows = body[:room]
    rows = rows + footer
    return rows[-height:] if height else []


def key_name(curses, code):
    """Name of a curses key code for `Wizard.handle_key`, or None for keys the wizard does not use."""
    named = {curses.KEY_UP: "up", curses.KEY_DOWN: "down", curses.KEY_LEFT: "left",
             curses.KEY_BACKSPACE: "backspace", curses.KEY_ENTER: "enter", 10: "enter", 13: "enter",
             32: "space", 8: "backspace", 127: "backspace"}
    if code in named:
        return named[code]
    if 0 < code < 256 and chr(code).isprintable():
        return chr(code).lower()
    return None


def _curses():
    try:
        import curses
    except ImportError:
        return None
    return curses


def _isatty(stream):
    try:
        return bool(stream.isatty())
    except (AttributeError, ValueError):
        return False


def can_run(stdin, stdout, environ=None):
    """True when the full-screen wizard can run here: both ends are a terminal, TERM is a real one and
    curses is importable. Anything else (pipe, TERM=dumb, no curses, `--text`) keeps the text mode."""
    environ = os.environ if environ is None else environ
    term = environ.get("TERM", "")
    if not term or term == "dumb":
        return False
    if not (_isatty(stdin) and _isatty(stdout)):
        return False
    return _curses() is not None


def draw(curses, window, view_data):
    """Paints one view. A terminal too small for it is clipped; a refused write is not an error."""
    attrs = {"title": curses.A_BOLD, "focus": curses.A_REVERSE, "hint": curses.A_DIM,
             "button": curses.A_BOLD}
    try:
        window.erase()
        height, width = window.getmaxyx()
        for index, (text, style) in enumerate(layout(view_data, width, height)):
            window.addnstr(index, 0, text, max(1, width - 1), attrs.get(style, 0))
        window.refresh()
    except curses.error:
        pass


def _session(curses, window, wizard, progress, perform):
    try:
        curses.curs_set(0)
    except curses.error:
        pass

    def paint():
        draw(curses, window, view(wizard, progress))

    progress.listener = paint
    while True:
        paint()
        action = wizard.handle_key(key_name(curses, window.getch()))
        if action == QUIT:
            return CANCELLED
        if action == INSTALL:
            wizard.goto(PROGRESS)
            paint()
            progress.finish(perform(copy.deepcopy(wizard.answers), progress))
            wizard.goto(DONE)
        elif action == CLOSE:
            return progress.code


def run(plan, perform):
    """Runs the wizard in a full-screen terminal and returns the exit code, or None when curses cannot
    start here (the caller falls back to the text mode). `perform(answers, progress)` installs and
    returns the exit code. After the screen closes, the install log is printed as plain text, because
    the full-screen view disappears with it."""
    curses = _curses()
    if curses is None:
        return None
    wizard, progress = Wizard(plan), Progress()
    started = []

    def main(window):
        started.append(True)
        return _session(curses, window, wizard, progress, perform)

    try:
        code = curses.wrapper(main)
    except curses.error:
        if started:
            raise
        return None
    if code == CANCELLED:
        print(t("installer.tui.cancelled"))
    else:
        print("\n".join(progress.rows()))
    return code
