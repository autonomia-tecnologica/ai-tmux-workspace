"""Flicker-free terminal viewer: redraws only the lines that changed.

No full-screen clear/refresh (that is what leaves character artifacts). Each panel provides
`collect(viewer) -> (header, body)` — run on a thread, from time to time — and the main loop only
draws, scrolls and handles keyboard/mouse.

Context menu: with `actions(viewer, key) -> [(label, active, action)]`, Space or a right click on an
item opens an overlay; arrows/j/k navigate, Enter runs `action(viewer, key)`, Esc closes.
An empty list does not open the menu. An inactive item is dimmed and Enter on it just closes.

Text input: `on_input(viewer, key)` runs before navigation and returns True when it consumed the key;
`footer(viewer)` draws the footer where the screen shows the text being typed. The footer wraps its
text and grows as needed, up to `MAX_FOOTER_LINES`; past that, it keeps the end visible (the cursor
of a message box is at the end).
"""
import os
import re
import select
import shutil
import signal
import sys
import termios
import threading
import tty
import unicodedata

import i18n
# colors live in colors.py; panels keep importing them from here (re-export)
from colors import (YELLOW, BLUE, B, BG, GRAY, DIM, MENU_BG, ORANGE, R, REV, GREEN,  # noqa: F401
                    RED)

ANSI = re.compile(r"\x1b\[[0-9;:?<=>]*[ -/]*[@-~]")
SGR = re.compile(rb"\x1b\[<(\d+);(\d+);(\d+)([Mm])")
ARROW_LEFT, ARROW_RIGHT = ("\x1b[D", "\x1bOD"), ("\x1b[C", "\x1bOC")


def direction(k):
    """"left" / "right" for the arrow keys and h / l; None for any other key."""
    if k in ARROW_LEFT or k == "h":
        return "left"
    if k in ARROW_RIGHT or k == "l":
        return "right"
    return None


MAX_FOOTER_LINES = 10    # the footer text grows up to this many rows, then keeps the tail


def _char_width(c):
    if unicodedata.combining(c):
        return 0
    return 2 if unicodedata.east_asian_width(c) in "WF" else 1


def clip(s, w):
    """Cut `s` to `w` visible columns, preserving ANSI sequences. -> (text, width)."""
    out, n, i = [], 0, 0
    while i < len(s):
        m = ANSI.match(s, i)
        if m:
            out.append(m.group())
            i = m.end()
            continue
        k = _char_width(s[i])
        if n + k > w:
            break
        out.append(s[i])
        n += k
        i += 1
    return "".join(out), n


def _width(s):
    """Visible columns of `s` (ignores ANSI)."""
    return sum(_char_width(c) for c in ANSI.sub("", s))


def ellipsize(s, w):
    """Fit `s` in `w` visible columns, ending a cut text with "…" (wide chars count as 2)."""
    if w <= 0:
        return ""
    if _width(s) <= w:
        return s
    return clip(s, w - 1)[0].rstrip() + "…"


class Row:
    __slots__ = ("text", "key", "cont")

    def __init__(self, text="", key=None, cont=False):
        # cont: continuation of the previous keyed line; not navigable, but highlighted with it
        self.text, self.key, self.cont = text, key, cont


def term_cols(default=80):
    """Terminal width when stdout is a tty, `default` otherwise."""
    return os.get_terminal_size().columns if sys.stdout.isatty() else default


def popup_header(title, keys, cols):
    """Two header rows for a popup screen: a bold orange bar (title + dim keys) over a full-width rule."""
    bar = f"{B}{ORANGE} {title} {R}{DIM} {keys}{R}"
    return [Row(bar), Row(f"{DIM}{'─' * cols}{R}")]


def close_on_esc(v, k):
    """Standard popup exit: Esc or q stops the viewer. -> True when the key closed it."""
    if k in ("esc", "q"):
        v.stopped = True
        v._wake()
        return True
    return False


class Viewer:
    def __init__(self, collect, interval=2.0, cursor=False, on_enter=None, initial_key=None,
                 on_key=None, on_focus_lost=None, footer=None, actions=None, wrap=False, on_input=None):
        self.collect, self.interval, self.cursor = collect, interval, cursor
        self.wrap = wrap        # up on the first row goes to the last, down on the last goes to the first
        self.on_enter, self.on_key, self.on_focus_lost = on_enter, on_key, on_focus_lost
        self.on_input = on_input  # on_input(viewer, key) -> True when it consumed the key (text input)
        self.footer = footer    # footer(viewer) -> full text of the selection, wrapped on a fixed footer
        self.actions = actions  # actions(viewer, key) -> [(label, active, action(viewer, key))]
        self.header, self.body, self._sig = [], [], None
        self.top, self.key, self.idx = 0, initial_key, 0
        self.redraw = threading.Event()
        self.stopped = False
        self._lock = threading.Lock()
        self._rp, self._wp = os.pipe()
        self._frame = None
        self._size = None
        self._footer_rows = 0    # rows taken by the footer (separator + text) in the last frame
        self.menu = None         # {"key", "items", "sel", "row", "column"} while the context menu is open
        self._box = None         # (x, y, width, height) of the menu box as last drawn

    # ---- collection (thread) ----------------------------------------------------------
    def _collector(self):
        while not self.stopped:
            try:
                header, body = self.collect(self)
            except Exception as e:      # a collection error must not take the panel down
                header, body = [], [Row(" " + i18n.t("viewer.error", e=e))]
            sig = (tuple((l.text, l.key) for l in header), tuple((l.text, l.key) for l in body))
            with self._lock:
                if sig != self._sig:
                    self._sig, self.header, self.body = sig, header, body
                    self._wake()
            self.redraw.wait(self.interval)
            self.redraw.clear()

    def _wake(self):
        try:
            os.write(self._wp, b"x")
        except OSError:
            pass

    # ---- drawing ----------------------------------------------------------------------
    def _locate_cursor(self):
        if not self.cursor:
            return
        sel = [i for i, l in enumerate(self.body) if l.key is not None]
        if not sel:
            self.idx = 0
            return
        for i in sel:
            if self.body[i].key == self.key:
                self.idx = i
                return
        self.idx = min(sel, key=lambda i: abs(i - self.idx))
        self.key = self.body[self.idx].key

    def _visible(self, h):
        if self.idx < self.top:
            self.top = self.idx
        elif self.idx >= self.top + h:
            self.top = self.idx - h + 1

    def _draw(self, force=False):
        cols, height = shutil.get_terminal_size((80, 24))
        with self._lock:
            header, body = self.header, self.body
        self._locate_cursor()
        rows = []
        for l in header[:height]:
            rows.append(clip(l.text, cols)[0])
        n_header = len(rows)
        footer_rows = []
        self._footer_rows = 0
        if self.footer:
            room = max(0, height - n_header - 1)     # at least the separator plus one text row
            if room:
                footer_rows = self._footer_lines(cols, min(room, MAX_FOOTER_LINES))
                self._footer_rows = len(footer_rows) + 1
        h = max(0, height - n_header - self._footer_rows)
        self.top = max(0, min(self.top, max(0, len(body) - h)))
        end = self.idx + 1
        while self.cursor and end < len(body) and body[end].cont:
            end += 1
        for r in range(h):
            i = self.top + r
            if i >= len(body):
                rows.append("")
                continue
            t, n = clip(body[i].text, cols)
            if self.cursor and self.idx <= i < end and (i == self.idx and body[i].key is not None or body[i].cont):
                t = BG + t.replace(R, R + BG) + " " * (cols - n) + R
            rows.append(t)
        self._box = None
        if self.menu is not None:
            self._overlay_menu(rows, n_header, h, cols)
        if self._footer_rows:
            rows = self._with_footer(rows, cols, height, footer_rows)
        while len(rows) < height:
            rows.append("")
        if force or self._frame is None or self._size != (cols, height):
            self._frame, self._size = [None] * height, (cols, height)
        buf = []
        for y, t in enumerate(rows):
            if self._frame[y] != t:
                buf.append(f"\x1b[{y + 1};1H{t}{R}\x1b[K")
                self._frame[y] = t
        if buf:
            sys.stdout.write("\x1b[?2026h" + "".join(buf) + "\x1b[?2026l")
            sys.stdout.flush()

    def _overlay_menu(self, rows, n_header, h, cols):
        """Draw the context menu as a box over the body, below the row it was opened from."""
        items = self.menu["items"]
        box_h = len(items)
        if h < box_h:
            return
        w = max(_width(label) for label, _, _ in items) + 2
        y0 = max(n_header, min(self.menu["row"] + 1, n_header + h - box_h))
        x0 = max(0, min(self.menu["column"], cols - w))
        for j, (label, active, _) in enumerate(items):
            if j == self.menu["sel"]:
                style = MENU_BG + B + REV
            else:
                style = MENU_BG + ("" if active else DIM)
            left, nl = clip(rows[y0 + j], x0)
            left += " " * (x0 - nl)
            text = " " + label + " " * (w - 1 - _width(label))
            rows[y0 + j] = left + R + style + text + R
        self._box = (x0, y0, w, box_h)

    def _footer_h(self):
        """Footer height from the last frame (0 when there is no footer)."""
        return self._footer_rows if self.footer else 0

    def _footer_lines(self, cols, room):
        """Footer text wrapped to the terminal width, at most `room` rows.

        A text longer than the room keeps its tail, because the message box shows the cursor at the
        end; the first visible row gets an ellipsis so the cut is visible."""
        try:
            text = self.footer(self) or ""
        except Exception:
            text = ""
        w = max(1, cols - 1)
        lines = []
        while text:
            chunk, _n = clip(text, w)
            if not chunk:                    # a single wide char that does not fit: never loop
                chunk, text = text[:1], text[1:]
            else:
                text = text[len(chunk):]
            lines.append(chunk)
        if len(lines) > room:
            lines = lines[-room:]
            mark = "… " if w >= 3 else "…"
            lines[0] = mark + clip(lines[0], max(1, w - _width(mark)))[0]
        return lines

    def _with_footer(self, rows, cols, height, lines):
        """Pad the body up to the footer area, then draw a separator and the footer rows."""
        rows = rows[:height - self._footer_rows]
        while len(rows) < height - self._footer_rows:
            rows.append("")
        rows.append(f"{GRAY}{'─' * cols}{R}")
        rows += [f"{DIM}{l}{R}" for l in lines]
        return rows

    # ---- input ------------------------------------------------------------------------
    def _move(self, d):
        sel = [i for i, l in enumerate(self.body) if l.key is not None]
        if not sel:
            return
        if d in ("home", "end"):
            self.idx = sel[0] if d == "home" else sel[-1]
        else:
            pos = min(range(len(sel)), key=lambda k: abs(sel[k] - self.idx))
            if self.wrap and d in (1, -1):
                self.idx = sel[(pos + d) % len(sel)]
            else:
                self.idx = sel[max(0, min(len(sel) - 1, pos + d))]
        self.key = self.body[self.idx].key
        h = max(1, (self._size or (80, 24))[1] - len(self.header) - self._footer_h())
        self._visible(h)
        self.redraw.set()    # panels that react to the selection (e.g. tasks) redraw now

    def _scroll(self, d):
        h = max(1, (self._size or (80, 24))[1] - len(self.header) - self._footer_h())
        self.top = max(0, min(self.top + d, max(0, len(self.body) - h)))

    def _open_menu(self, row, column):
        """Open the context menu for the selected item (row: screen row, 0-based). No actions, no menu."""
        if not self.actions or not (0 <= self.idx < len(self.body)) or self.body[self.idx].key is None:
            return
        key = self.body[self.idx].key
        items = self.actions(self, key)
        if items:
            self.menu = {"key": key, "items": items, "sel": 0, "row": row, "column": column}

    def _run_menu(self, j):
        m = self.menu
        self.menu = None
        _, active, action = m["items"][j]
        if active:
            action(self, m["key"])
            self.redraw.set()

    def _menu_key(self, k):
        m = self.menu
        n = len(m["items"])
        if k in ("j", "\x1b[B", "\x1bOB"):
            m["sel"] = (m["sel"] + 1) % n
        elif k in ("k", "\x1b[A", "\x1bOA"):
            m["sel"] = (m["sel"] - 1) % n
        elif k in ("\r", "\n"):
            self._run_menu(m["sel"])
        elif k == "esc":
            self.menu = None

    def _key(self, k):
        if self.menu is not None:
            self._menu_key(k)
            return
        if self.on_input and self.on_input(self, k):
            return
        h = max(1, (self._size or (80, 24))[1] - len(self.header) - self._footer_h())
        if k in ("j", "\x1b[B", "\x1bOB"):
            self._move(1) if self.cursor else self._scroll(1)
        elif k in ("k", "\x1b[A", "\x1bOA"):
            self._move(-1) if self.cursor else self._scroll(-1)
        elif k in ("\x1b[6~", "\x04"):
            self._move(h // 2) if self.cursor else self._scroll(h - 1)
        elif k in ("\x1b[5~", "\x15"):
            self._move(-(h // 2)) if self.cursor else self._scroll(-(h - 1))
        elif k in ("g", "\x1b[H", "\x1b[1~"):
            self._move("home") if self.cursor else setattr(self, "top", 0)
        elif k in ("G", "\x1b[F", "\x1b[4~"):
            self._move("end") if self.cursor else self._scroll(10 ** 6)
        elif k in ("\r", "\n", "l") + (() if self.actions else (" ",)) and self.cursor and self.on_enter:
            if 0 <= self.idx < len(self.body) and self.body[self.idx].key is not None:
                self.on_enter(self, self.body[self.idx].key, k)
        elif k == " " and self.cursor:
            self._open_menu(len(self.header) + self.idx - self.top, 2)
        elif k == "r":
            self.redraw.set()
        elif k == "\x1b[O":     # focus lost (tmux focus-events)
            if self.on_focus_lost:
                self.on_focus_lost(self)
        elif k == "\x1b[I":
            pass
        elif self.on_key:
            self.on_key(self, direction(k) or k)

    def _index_at(self, row):
        """Index in the body of the selectable row at screen row `row` (0-based), or None."""
        h = max(0, (self._size or (80, 24))[1] - len(self.header) - self._footer_h())
        if row < len(self.header) or row - len(self.header) >= h:
            return None
        i = self.top + (row - len(self.header))
        if 0 <= i < len(self.body) and self.body[i].key is not None:
            return i
        return None

    def _mouse(self, b, x, y, press):
        row, column = y - 1, x - 1
        if b == 64:
            self._scroll(-3)
        elif b == 65:
            self._scroll(3)
        elif press and b in (0, 2) and self.menu is not None:
            if b == 0 and self._box:
                cx, cy, cw, ca = self._box
                if cx <= column < cx + cw and cy <= row < cy + ca:
                    self._run_menu(row - cy)
                    return
            self.menu = None        # click outside the box closes the menu
        elif b == 0 and press and self.cursor and self.on_enter:
            i = self._index_at(row)
            if i is not None:
                self.idx, self.key = i, self.body[i].key
                self.redraw.set()
                self.on_enter(self, self.key, "click")
        elif b == 2 and press and self.cursor and self.actions:
            i = self._index_at(row)
            if i is not None:
                self.idx, self.key = i, self.body[i].key
                self.redraw.set()
                self._open_menu(row, column)

    def _process(self, data):
        i = 0
        while i < len(data):
            m = SGR.match(data, i)
            if m:
                self._mouse(int(m.group(1)), int(m.group(2)), int(m.group(3)), m.group(4) == b"M")
                i = m.end()
                continue
            if data[i:i + 1] == b"\x1b":
                m = re.compile(rb"\x1b(\[[0-9;]*[~A-Za-z]|O[A-Za-z])").match(data, i)
                if m:
                    self._key(m.group().decode("latin1"))
                    i = m.end()
                    continue
                self._key("esc")      # lone Esc key
                i += 1
                continue
            j = i + 1
            while j < len(data) and data[j] & 0xC0 == 0x80:
                j += 1
            self._key(data[i:j].decode("utf-8", "replace"))
            i = j

    # ---- main loop --------------------------------------------------------------------
    def run(self):
        fd = sys.stdin.fileno()
        old = termios.tcgetattr(fd)
        signal.signal(signal.SIGWINCH, lambda *_: self._wake())
        signal.signal(signal.SIGTERM, lambda *_: setattr(self, "stopped", True) or self._wake())
        tty.setcbreak(fd)
        sys.stdout.write("\x1b[?1049h\x1b[?25l\x1b[?1000h\x1b[?1006h\x1b[?1004h")
        sys.stdout.flush()
        threading.Thread(target=self._collector, daemon=True).start()
        try:
            while not self.stopped:
                self._draw()
                ready, _, _ = select.select([fd, self._rp], [], [], 5)
                if self._rp in ready:
                    os.read(self._rp, 4096)
                if fd in ready:
                    d = os.read(fd, 4096)
                    if not d:
                        break
                    self._process(d)
        finally:
            self.stopped = True
            sys.stdout.write("\x1b[?1004l\x1b[?1006l\x1b[?1000l\x1b[?25h\x1b[?1049l")
            sys.stdout.flush()
            termios.tcsetattr(fd, termios.TCSADRAIN, old)
