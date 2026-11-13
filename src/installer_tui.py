"""Full-screen installer wizard for `install.sh` ("next, next, finish"): screens, keys and drawing.

Stdlib only (curses). Three layers in one file, kept apart on purpose:

- screen logic (`Plan`, `Wizard`, `Progress`, `summary_lines`, `view`): plain data and key handling,
  no terminal, tested directly;
- layout (`layout`): turns a view into a centred box of cells for a terminal size, also without curses;
- drawing (`draw`, `key_name`, `run`): the only code that talks to curses, and only when `can_run`
  says a terminal can take it.

The screen is a dialog-style box: a title, the text (which scrolls with PgUp/PgDn when it is long), the
options (↑/↓ or k/j move, Enter confirms, Space marks in the checklist) and the footer buttons (Tab
switches between the list and the buttons, ←/→ move between buttons, Enter presses the focused one).

This module never imports `installer` (that would go up a layer, ARCH.md): the caller passes a `Plan`
with the texts and paths it computed, and a `perform(answers, progress)` function that installs. The
answers are collected on the screens and nothing is written until the summary's "Instalar". Every
end-user text comes from i18n/pt-BR.json (`installer.tui.*`, plus the `installer.deps.*` lines).
"""
import copy
import os
import sys
import textwrap
from dataclasses import dataclass

from i18n import t

WELCOME, DEPS, MANAGE, HARNESS, PROVIDERS, DEFAULT, SUMMARY, PROGRESS, DONE = (
    "welcome", "deps", "manage", "harness", "providers", "default", "summary", "progress", "done")
HELP_KEYS = {MANAGE: "installer.tui.manage.help", HARNESS: "installer.tui.harness.help",
             PROVIDERS: "installer.tui.providers.help", DEFAULT: "installer.tui.default.help"}

# Where the keyboard focus is: the option list, or the footer buttons.
LIST, BUTTONS = "list", "buttons"
# What a footer button does (Wizard.buttons pairs each one with its label).
BACK, NEXT, CANCEL, INSTALL_ACTION, CLOSE_ACTION = "back", "next", "cancel", "install", "close"

# What handle_key returns for the drawing loop to act on.
QUIT, INSTALL, CLOSE = "quit", "install", "close"
CANCELLED = 1           # exit code of a cancelled wizard: the same as the text mode's abort

# The box needs this much terminal; below it the wizard warns (and, before the first screen, the text
# mode takes over). The box is never wider than BOX_MAX_WIDTH.
MIN_WIDTH, MIN_HEIGHT = 60, 18
BOX_MAX_WIDTH = 72
PAGE = 5                # lines PgUp/PgDn move when the drawing loop does not say how many fit


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
    """The answers, the current screen and the keyboard focus. Key names come from `key_name`; see
    `handle_key`."""

    def __init__(self, plan):
        self.plan = plan
        self.answers = {"manage": True, "harness_managed": True, "providers": [], "default": None}
        self.screen = WELCOME
        self.cursor = 0         # index in the option list
        self.focus = BUTTONS
        self.button = 0         # index in the footer buttons
        self.scroll = None      # first text line shown; None until the drawing loop places it
        self.goto(WELCOME)

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

    def buttons(self):
        """(action, label) of the footer buttons of the current screen, left to right."""
        back = (BACK, t("installer.tui.btn.back"))
        nxt = (NEXT, t("installer.tui.btn.next"))
        if self.screen == WELCOME:
            return [(CANCEL, t("installer.tui.btn.cancel")), nxt]
        if self.screen == SUMMARY:
            return [back, (INSTALL_ACTION, t("installer.tui.btn.install"))]
        if self.screen == DONE:
            return [(CLOSE_ACTION, t("installer.tui.btn.finish"))]
        if self.screen == PROGRESS:
            return []
        return [back, nxt]

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
        # The list has the focus when the screen has one; the default button is the last (Próximo,
        # Instalar, Concluir), so Enter moves on the way it always did.
        self.focus = LIST if self.options() else BUTTONS
        self.button = max(0, len(self.buttons()) - 1)
        self.scroll = None

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

    def handle_key(self, key, page=PAGE):
        """Applies one key name (`up`/`k`, `down`/`j`, `space`, `tab`, `left`, `right`, `pgup`, `pgdn`,
        `enter`, `backspace`, `b`, `q`) and returns QUIT (cancel before any install), INSTALL (summary
        confirmed), CLOSE (the final screen closed) or None. Nothing is installed here: the caller acts
        on the result. `page` is how many text lines PgUp/PgDn move.
        """
        if self.screen == PROGRESS:
            return None
        if self.screen == DONE and key == "q":
            return CLOSE
        if key == "q":          # Esc is not a cancel: an arrow key split by the terminal must not quit
            return QUIT
        options, buttons = self.options(), self.buttons()
        if key in ("pgup", "pgdn"):
            here = self.scroll or 0
            self.scroll = max(0, here - page) if key == "pgup" else here + page
        elif key == "tab" and options:
            self.focus = BUTTONS if self.focus == LIST else LIST
        elif key in ("left", "right") and buttons:
            self.focus = BUTTONS
            step = -1 if key == "left" else 1
            self.button = min(max(0, self.button + step), len(buttons) - 1)
        elif key in ("up", "k") and options:
            self.focus = LIST
            self.cursor = max(0, self.cursor - 1)
        elif key in ("down", "j") and options:
            self.focus = LIST
            self.cursor = min(len(options) - 1, self.cursor + 1)
        elif key == "space" and options and self.focus == LIST:
            self._mark(options[self.cursor][0])
        elif key in ("b", "backspace"):
            self.back()
        elif key == "enter":
            if options and self.focus == LIST:
                if self.screen != PROVIDERS:
                    self._mark(options[self.cursor][0])
                return self.next()
            if buttons:
                return self._press(buttons[self.button][0])
        return None

    def _press(self, action):
        if action == BACK:
            self.back()
            return None
        if action == CANCEL:
            return QUIT
        if action == INSTALL_ACTION:
            return INSTALL
        if action == CLOSE_ACTION:
            return CLOSE
        return self.next()

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
    """The current screen as data: title, text lines, option rows (label, mark, focused), hint, footer
    buttons (`buttons` labels, `actions` in the same order), and where the focus is (`focus`, `button`).

    `tail` says the text keeps its end when the box is too short (the install log)."""
    screen, plan = wizard.screen, wizard.plan
    # The final screen's title depends on the result (done_ok / done_failed), set in its branch below.
    result = {"title": "" if screen == DONE else t("installer.tui.title." + screen), "lines": [],
              "options": [], "hint": "", "buttons": [], "actions": [], "focus": wizard.focus,
              "button": wizard.button, "tail": False}
    if screen == DONE:
        ok = progress.code == 0
        result.update(title=t("installer.tui.title.done_ok" if ok else "installer.tui.title.done_failed"),
                      lines=progress.rows(), tail=True,
                      hint=t("installer.tui.hint.done" if ok else "installer.tui.hint.failed"))
    elif screen == PROGRESS:
        result.update(lines=progress.rows(), tail=True, hint=t("installer.tui.hint.progress"))
    elif screen == WELCOME:
        result.update(lines=t("installer.tui.welcome.text").split("\n"), hint=t("installer.tui.hint.nav"))
    elif screen == DEPS:
        result.update(lines=list(plan.dependency_lines), hint=t("installer.tui.hint.nav"))
    elif screen == SUMMARY:
        result.update(lines=summary_lines(wizard), hint=t("installer.tui.hint.nav"))
    else:
        radio = screen != PROVIDERS
        result.update(lines=[t(HELP_KEYS[screen])],
                      hint=t("installer.tui.hint.radio" if radio else "installer.tui.hint.providers"))
        for index, (value, label) in enumerate(wizard.options()):
            marked = wizard.is_marked(screen, value)
            if radio:
                mark = "(*)" if marked else "( )"
            else:
                mark = "[x]" if marked else "[ ]"
            result["options"].append((label, mark, index == wizard.cursor))
    buttons = wizard.buttons()
    result["actions"] = [action for action, _label in buttons]
    result["buttons"] = [label for _action, label in buttons]
    return result


def _wrap(text, width):
    """The text wrapped to `width`; a continuation line keeps the paragraph's leading indent."""
    indent = len(text) - len(text.lstrip(" "))
    if indent >= width:
        indent = 0
    return textwrap.wrap(text, width, subsequent_indent=" " * indent) or [""]


def _button_row(view_data, inner_w):
    labels = [f"< {label} >" for label in view_data["buttons"]]
    total = sum(len(label) for label in labels) + 2 * (len(labels) - 1)
    row = [(" " * max(0, (inner_w - total) // 2), "normal")]
    for index, label in enumerate(labels):
        if index:
            row.append(("  ", "normal"))
        focused = view_data["focus"] == BUTTONS and index == view_data["button"]
        row.append((label, "focus" if focused else "button"))
    return row


def layout(view_data, width, height, scroll=None):
    """The box of a view in a terminal of `width` x `height`, as data: `top`, `left`, `width`, `height`
    (the box, centred), `cells` ((y, x, text, style) to paint, styles: title, focus, cursor, hint, button,
    normal), `scroll` (the first text line shown, clamped; `scroll` None means the top, or the bottom on
    a tail screen) and `room` (how many text lines fit, for paging).

    The text between the title and the options scrolls; the options and the footer always stay. A
    terminal too short for the whole box gets a shorter box that scrolls, never a box that overflows.
    """
    width, height = max(1, width), max(1, height)
    box_w = min(BOX_MAX_WIDTH, max(1, width - 4))
    inner_w = max(1, box_w - 4)                   # border and one blank column on each side
    head = [[(view_data["title"], "title")], [("", "normal")]]
    text = [[(row, "normal")] for line in view_data["lines"] for row in _wrap(line, inner_w)]
    middle = []
    if view_data["options"]:
        middle.append([("", "normal")])
        for label, mark, focused in view_data["options"]:
            index_mark = ">" if focused else " "
            if focused:
                style = "focus" if view_data["focus"] == LIST else "cursor"
            else:
                style = "normal"
            middle.append([(f"{index_mark} {mark} {label}"[:inner_w], style)])
    foot = [[("", "normal")]] + [[(row, "hint")] for row in _wrap(view_data["hint"], inner_w)]
    if view_data["buttons"]:
        foot.append(_button_row(view_data, inner_w))
    natural = len(head) + len(text) + len(middle) + len(foot)
    margin_height = height - 2 if height > 4 else height
    box_h = min(natural + 2, margin_height)
    box_w = min(box_w, width)
    inner_h = max(0, box_h - 2)
    room = max(0, inner_h - len(head) - len(middle) - len(foot))
    max_start = max(0, len(text) - room)
    if scroll is None:
        start = max_start if view_data["tail"] else 0
    else:
        start = min(max(0, scroll), max_start)
    rows = head + text[start:start + room] + middle + foot
    rows = rows[-inner_h:] if inner_h else []
    top, left = (height - box_h) // 2, (width - box_w) // 2
    cells = []
    for index, segments in enumerate(rows):
        x = left + 2
        for segment, style in segments:
            cells.append((top + 1 + index, x, segment, style))
            x += len(segment)
    return {"top": top, "left": left, "width": box_w, "height": box_h, "cells": cells,
            "scroll": start, "room": room}


def too_small_text(width, height):
    return t("installer.tui.too_small", cols=width, rows=height, min_cols=MIN_WIDTH, min_rows=MIN_HEIGHT)


def key_name(curses, code):
    """Name of a curses key code for `Wizard.handle_key`, or None for keys the wizard does not use."""
    named = {curses.KEY_UP: "up", curses.KEY_DOWN: "down", curses.KEY_LEFT: "left",
             curses.KEY_RIGHT: "right", curses.KEY_PPAGE: "pgup", curses.KEY_NPAGE: "pgdn",
             curses.KEY_BACKSPACE: "backspace", curses.KEY_ENTER: "enter", 10: "enter", 13: "enter",
             9: "tab", 32: "space", 8: "backspace", 127: "backspace"}
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


def _attributes(curses):
    return {"title": curses.A_BOLD, "focus": curses.A_REVERSE, "cursor": curses.A_BOLD,
            "hint": curses.A_DIM, "button": curses.A_BOLD}


def _try(curses, call, *args):
    """A curses write that the terminal refuses (the last cell of the screen, a shrink) is not an error."""
    try:
        call(*args)
    except curses.error:
        pass


def _frame(curses, window, geometry):
    top, left = geometry["top"], geometry["left"]
    width, height = geometry["width"], geometry["height"]
    if width < 2 or height < 2:
        return
    bottom, right = top + height - 1, left + width - 1
    _try(curses, window.hline, top, left + 1, curses.ACS_HLINE, width - 2)
    _try(curses, window.hline, bottom, left + 1, curses.ACS_HLINE, width - 2)
    _try(curses, window.vline, top + 1, left, curses.ACS_VLINE, height - 2)
    _try(curses, window.vline, top + 1, right, curses.ACS_VLINE, height - 2)
    _try(curses, window.addch, top, left, curses.ACS_ULCORNER)
    _try(curses, window.addch, top, right, curses.ACS_URCORNER)
    _try(curses, window.addch, bottom, left, curses.ACS_LLCORNER)
    _try(curses, window.addch, bottom, right, curses.ACS_LRCORNER)


def draw(curses, window, view_data, scroll=None):
    """Paints one view as a box centred in the window and returns its layout (the caller keeps the
    clamped scroll and the page size from it). A window too small for the box shows the warning instead
    and returns None. A refused write is not an error."""
    try:
        window.erase()
        height, width = window.getmaxyx()
    except curses.error:
        return None
    if width < MIN_WIDTH or height < MIN_HEIGHT:
        _try(curses, window.addnstr, 0, 0, too_small_text(width, height), max(1, width - 1))
        _try(curses, window.refresh)
        return None
    geometry = layout(view_data, width, height, scroll)
    attributes = _attributes(curses)
    _frame(curses, window, geometry)
    for y, x, text, style in geometry["cells"]:
        _try(curses, window.addnstr, y, x, text, len(text), attributes.get(style, 0))
    _try(curses, window.refresh)
    return geometry


def _session(curses, window, wizard, progress, perform):
    try:
        curses.curs_set(0)
    except curses.error:
        pass
    state = {"page": PAGE}

    def paint():
        geometry = draw(curses, window, view(wizard, progress), wizard.scroll)
        if geometry is not None:
            wizard.scroll = geometry["scroll"]
            state["page"] = max(1, geometry["room"] - 1)

    progress.listener = paint
    while True:
        paint()
        action = wizard.handle_key(key_name(curses, window.getch()), page=state["page"])
        if action == QUIT:
            return CANCELLED
        if action == INSTALL:
            wizard.goto(PROGRESS)
            paint()
            progress.finish(perform(copy.deepcopy(wizard.answers), progress))
            wizard.goto(DONE)
        elif action == CLOSE:
            return progress.code


def _terminal_size():
    """(columns, lines) of the terminal on stdout; when it cannot be read, the minimum (no warning)."""
    try:
        columns, lines = os.get_terminal_size(sys.stdout.fileno())
    except (OSError, ValueError, AttributeError):
        return MIN_WIDTH, MIN_HEIGHT
    return columns, lines


def run(plan, perform):
    """Runs the wizard in a full-screen terminal and returns the exit code, or None when curses cannot
    start here or the terminal is smaller than the box (the caller then uses the text mode).
    `perform(answers, progress)` installs and returns the exit code. After the screen closes, the install
    log is printed as plain text, because the full-screen view disappears with it."""
    curses = _curses()
    if curses is None:
        return None
    columns, lines = _terminal_size()
    if columns < MIN_WIDTH or lines < MIN_HEIGHT:
        print(too_small_text(columns, lines))
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
