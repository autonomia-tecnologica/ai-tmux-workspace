#!/usr/bin/env python3
"""Records the README demo from the workspace running on its own tmux server, then renders
docs/demo.gif and docs/image.png (a frame of the workspace in use). Called by make-demo.sh, which
sets the environment (TMUX_TMPDIR, AT_WS_CONFIG, WS_ESTADO, HOME) and starts the workspace.
Do not run it against the user's workspace.

Method: each frame is the window composed from `tmux capture-pane -e` of its panes (plus the
status line, from the real status formats), as a grid of 160 x 46 cells, drawn with Hack Nerd
Font Mono by Pillow. Frames are taken only at named points of the script (checkpoints), after the
panels had a second to redraw, so the GIF does not depend on timing: two runs give the same file.
Waiting time of the fake agent is not recorded: a checkpoint holds for the scene's fixed seconds.
"""
import os
import re
import subprocess
import sys
import time
import unicodedata
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

os.environ.setdefault("WS_DIR", str(Path(__file__).resolve().parent.parent))   # statusbar.py is run from here

SESSION = "claude"      # the launcher's session name for profile "claude"
SOCKET = "ws-claude"    # the launcher's socket name; TMUX_TMPDIR (set by make-demo.sh) isolates it
COLS, ROWS = 160, 46
CW, CH = 8, 17          # cell size in px: 1280 x 782 output
GIF_LIMIT_S = 20.0
SETTLE_S = 1.2          # the panels redraw once a second: wait this long before capturing after an action

DEF_FG = "#d0d0d0"
BG_ACTIVE = "#0f2339"   # window-active-style of config/tmux.conf
BG_IDLE = "#0b0d10"
BORDER = "#4e4e4e"
STATUS_BG = "#262626"
STATUS_FG = "#bcbcbc"
DOTS = {"🟢": "#3fb950", "🟡": "#d29922", "🔴": "#f85149", "🟠": "#db6d28"}
BASIC = ["#000000", "#cd0000", "#00cd00", "#cdcd00", "#0000ee", "#cd00cd", "#00cdcd", "#e5e5e5",
         "#7f7f7f", "#ff0000", "#00ff00", "#ffff00", "#5c5cff", "#ff00ff", "#00ffff", "#ffffff"]

DEFAULT_STYLE = (None, None, False, False, False, False)   # fg, bg, bold, dim, reverse, underline
TOKEN = re.compile(r"\x1b\[([0-9;:]*)m|\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)"
                   r"|\x1b[()][0-9A-Za-z]|\x1b[=>78]")


def tmux(*args, check=True):
    r = subprocess.run(["tmux", "-L", SOCKET, *args], capture_output=True, text=True)
    if check and r.returncode:
        raise RuntimeError(f"tmux {' '.join(args)}: {r.stderr.strip()}")
    return r.stdout


# --- ANSI and colors -------------------------------------------------------------------------------
def c256(n):
    if n < 16:
        return BASIC[n]
    if n < 232:
        n -= 16
        levels = [0 if v == 0 else 55 + 40 * v for v in (n // 36, (n // 6) % 6, n % 6)]
        return "#%02x%02x%02x" % tuple(levels)
    v = 8 + 10 * (n - 232)
    return "#%02x%02x%02x" % (v, v, v)


def apply_sgr(style, params):
    fg, bg, bold, dim, rev, ul = style
    p = [int(x) if x else 0 for x in re.split(r"[;:]", params or "0")]
    i = 0
    while i < len(p):
        c = p[i]
        if c == 0:
            fg, bg, bold, dim, rev, ul = None, None, False, False, False, False
        elif c == 1:
            bold = True
        elif c == 2:
            dim = True
        elif c == 4:
            ul = True
        elif c == 7:
            rev = True
        elif c == 22:
            bold = dim = False
        elif c == 24:
            ul = False
        elif c == 27:
            rev = False
        elif c == 39:
            fg = None
        elif c == 49:
            bg = None
        elif 30 <= c <= 37:
            fg = BASIC[c - 30]
        elif 90 <= c <= 97:
            fg = BASIC[c - 82]
        elif 40 <= c <= 47:
            bg = BASIC[c - 40]
        elif 100 <= c <= 107:
            bg = BASIC[c - 92]
        elif c in (38, 48) and i + 1 < len(p):
            col = None
            if p[i + 1] == 5 and i + 2 < len(p):
                col, i = c256(p[i + 2]), i + 2
            elif p[i + 1] == 2 and i + 4 < len(p):
                col, i = "#%02x%02x%02x" % tuple(p[i + 2:i + 5]), i + 4
            else:
                break
            if c == 38:
                fg = col
            else:
                bg = col
        i += 1
    return (fg, bg, bold, dim, rev, ul)


def parse_line(line):
    """[(char, style)] for one captured line; SGR is applied, other escape sequences are dropped."""
    cells, style, pos = [], DEFAULT_STYLE, 0
    for m in TOKEN.finditer(line):
        cells += [(ch, style) for ch in line[pos:m.start()]]
        pos = m.end()
        if m.group(1) is not None:
            style = apply_sgr(style, m.group(1))
    cells += [(ch, style) for ch in line[pos:]]
    return cells


def cell_width(ch):
    if ch in DOTS or unicodedata.east_asian_width(ch) in ("W", "F"):
        return 2
    if unicodedata.combining(ch) or ch in "️‍":
        return 0
    return 1


# --- composing a window ----------------------------------------------------------------------------
def capture_pane(pane_id):
    return tmux("capture-pane", "-e", "-p", "-t", pane_id).split("\n")


def plain_pane(pane_id):
    return tmux("capture-pane", "-p", "-t", pane_id)


STYLE_TAG = re.compile(r"#\[[^\]]*\]")
SHELL_PART = re.compile(r"#\(([^)]*)\)")   # tmux runs these; display -p does not


def expand_format(window, fmt):
    """Text of a status format: tmux expands the #{...} parts, and each #(...) runs its command
    (the workspace's statusbar.py) with the same arguments tmux would pass."""
    out, pos = [], 0
    for m in SHELL_PART.finditer(fmt):
        out.append(tmux("display", "-p", "-t", window, fmt[pos:m.start()]).rstrip("\n"))
        cmd = tmux("display", "-p", "-t", window, m.group(1)).strip()
        out.append(subprocess.run(cmd, shell=True, capture_output=True, text=True, check=False).stdout.strip())
        pos = m.end()
    out.append(tmux("display", "-p", "-t", window, fmt[pos:]).rstrip("\n"))
    return "".join(out)


def status_row(window):
    """Plain text of the status line, from the real status-left/right formats. There is no client,
    so #{client_width} is replaced by the width the recording uses; style tags are dropped."""
    out = []
    for option in ("status-left", "status-right"):
        fmt = tmux("show", "-gv", option).rstrip("\n").replace("#{client_width}", str(COLS))
        out.append(STYLE_TAG.sub("", expand_format(window, fmt)).strip())
    return out[0], out[1]


def compose(window):
    """Grid of COLS x ROWS cells: (char, fg, bg, bold, dim, underline) with concrete colors."""
    width, height = (int(v) for v in tmux("display", "-p", "-t", window, "#{window_width} #{window_height}").split())
    assert (width, height) == (COLS, ROWS), f"window is {width}x{height}, expected {COLS}x{ROWS}"
    grid = [[None] * COLS for _ in range(ROWS)]
    covered = [[False] * COLS for _ in range(ROWS)]
    rows = tmux("list-panes", "-t", window, "-F",
                "#{pane_id}|#{pane_left}|#{pane_top}|#{pane_width}|#{pane_height}|#{pane_active}")
    for line in rows.strip().split("\n"):
        pane_id, left, top, pw, ph, active = line.split("|")
        left, top, pw, ph = int(left), int(top), int(pw), int(ph)
        bg_default = BG_ACTIVE if active == "1" else BG_IDLE
        for y in range(top, top + ph):
            for x in range(left, left + pw):
                grid[y][x] = (" ", DEF_FG, bg_default, False, False, False)
                covered[y][x] = True
        for y, text in enumerate(capture_pane(pane_id)[:ph]):
            x = 0
            for ch, (fg, bg, bold, dim, rev, ul) in parse_line(text):
                w = cell_width(ch)
                if w == 0 or x >= pw:
                    continue
                if rev:
                    fg, bg = bg or bg_default, fg or DEF_FG
                cell = (ch, fg or DEF_FG, bg or bg_default, bold, dim, ul)
                grid[top + y][left + x] = cell
                if w == 2 and x + 1 < pw:
                    grid[top + y][left + x + 1] = ("", cell[1], cell[2], bold, dim, ul)
                x += w
    # the rest is tmux's border: heavy lines between panes
    for y in range(ROWS):
        for x in range(COLS):
            if covered[y][x] or y == 0:
                continue
            horiz = x > 0 and x < COLS - 1 and covered[y][x - 1] and covered[y][x + 1]
            vert = y > 0 and y < ROWS - 1 and covered[y - 1][x] and covered[y + 1][x]
            glyph = "┃" if horiz else "━" if vert else " "
            grid[y][x] = (glyph, BORDER, BG_IDLE, False, False, False)
    left, right = status_row(window)
    line = list(left[:COLS].ljust(COLS))
    if right:
        right = right[-COLS:]
        line[COLS - len(right):] = list(right)
    for x in range(COLS):
        grid[0][x] = (line[x], STATUS_FG, STATUS_BG, False, False, False)
    return tuple(tuple(row) for row in grid)


# --- drawing ---------------------------------------------------------------------------------------
FONT_FAMILY = "Hack Nerd Font Mono"   # has the icons the panels use; fallbacks come per character


def font_file(pattern):
    return subprocess.run(["fc-match", "-f", "%{file}", pattern], capture_output=True, text=True,
                          check=True).stdout


def blend(fg, bg, t=0.5):
    a = [int(fg[i:i + 2], 16) for i in (1, 3, 5)]
    b = [int(bg[i:i + 2], 16) for i in (1, 3, 5)]
    return "#%02x%02x%02x" % tuple(round(x * t + y * (1 - t)) for x, y in zip(a, b))


class Renderer:
    def __init__(self):
        self.fonts = {}          # (file, bold) -> font
        self.by_char = {}        # (char, bold) -> font, found with fontconfig's charset match
        self.regular = self.font(font_file(f"{FONT_FAMILY}"), False)
        self.bold = self.font(font_file(f"{FONT_FAMILY}:bold"), True)

    def font(self, path, bold):
        key = (path, bold)
        if key not in self.fonts:
            self.fonts[key] = ImageFont.truetype(path, 13)
        return self.fonts[key]

    def font_for(self, ch, bold):
        """The bold or regular font if it has the glyph, else the first installed font that has it."""
        key = (ch, bold)
        if key not in self.by_char:
            self.by_char[key] = self.font(font_file(f"{FONT_FAMILY}:charset={ord(ch):x}"), bold)
        return self.by_char[key]

    def draw(self, grid):
        img = Image.new("RGB", (COLS * CW, ROWS * CH), BG_IDLE)
        d = ImageDraw.Draw(img)
        for y, row in enumerate(grid):
            for x, (ch, fg, bg, bold, dim, ul) in enumerate(row):
                px, py = x * CW, y * CH
                d.rectangle([px, py, px + CW - 1, py + CH - 1], fill=bg)
                if ch in DOTS:
                    r = CH * 0.3
                    cx, cy = px + CW, py + CH / 2
                    d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=DOTS[ch])
                    continue
                if dim:
                    fg = blend(fg, bg)
                if ch.strip():
                    d.text((px, py + 13), ch, font=self.font_for(ch, bold), fill=fg, anchor="ls")
                if ul:
                    d.line([px, py + CH - 2, px + CW - 1, py + CH - 2], fill=fg)
        return img


# --- recording -------------------------------------------------------------------------------------
class Recording:
    def __init__(self):
        self.frames = []        # [grid, hold_seconds]
        self.shot = None        # grid of the static image (docs/image.png)
        self.window = None

    def snap(self, hold, extend=True):
        """Captures the active window. Equal to the last frame: its hold grows if extend, else nothing."""
        grid = compose(self.window or f"{SESSION}:")
        if self.frames and self.frames[-1][0] == grid:
            if extend:
                self.frames[-1][1] += hold
            return grid
        self.frames.append([grid, hold])
        return grid

    def settle(self, hold):
        """Captures after the panels had time to redraw (they refresh once a second)."""
        time.sleep(SETTLE_S)
        return self.snap(hold)

    def checkpoint(self, pane_id, needle, hold, delay=SETTLE_S):
        """Waits (without capturing) until needle is on the pane, then waits delay and captures.
        Only named points of the script become frames, so the GIF does not depend on timing."""
        wait_text(pane_id, needle)
        time.sleep(delay)
        return self.snap(hold)


def wait_text(pane_id, needle, timeout=90.0):
    end = time.time() + timeout
    while time.time() < end:
        if needle in plain_pane(pane_id):
            return
        time.sleep(0.2)
    raise RuntimeError(f"timeout waiting for {needle!r} in {pane_id}")


def panes_of(window):
    out = tmux("list-panes", "-t", window, "-F", "#{pane_id}|#{@papel}")
    return {role: pid for pid, role in (l.split("|") for l in out.strip().split("\n"))}


def window_id(name):
    for line in tmux("list-windows", "-t", SESSION, "-F", "#{window_id}|#{window_name}").strip().split("\n"):
        wid, wname = line.split("|", 1)
        if wname == name:
            return wid
    return None


def send(pane_id, *names):
    """Keys typed into a pane. send-keys reaches the program directly, so it does not run key bindings."""
    tmux("send-keys", "-t", pane_id, *names)


def literal(pane_id, text):
    tmux("send-keys", "-t", pane_id, "-l", text)


def scene_script(rec):
    """The scenes, in order. Holds are seconds of GIF time.

    send-keys does not run tmux key bindings, so each bound shortcut is shown with the action it runs:
    S-Left is `select-pane -L`, prefix+1 is `select-window`; typing into a focused panel is send-keys.
    """
    main = window_id("lojinha-api")
    panes = panes_of(main)
    rec.window = main
    # No client is attached, so windows created now would get tmux's default 80x24 and the phone layout.
    tmux("set-option", "-g", "default-size", f"{COLS}x{ROWS}")
    wait_text(panes["claude"], "agente simulado da demo: pronto")
    time.sleep(SETTLE_S)
    rec.snap(1.6)                                            # 1. the workspace: repos, tasks, agent
    tmux("select-pane", "-L", "-t", panes["claude"])         # S-Left: focus the repos panel (no mouse)
    send(panes["repos"], "j")                                # onto the LOJINHA group row
    send(panes["repos"], "j")                                # onto lojinha-web (Enter on a group folds it)
    send(panes["repos"], "Enter")                            # open lojinha-web from the list
    deadline = time.time() + 90
    while window_id("lojinha-web") is None:
        if time.time() > deadline:
            raise RuntimeError("lojinha-web did not open")
        time.sleep(0.2)
    rec.window = web = window_id("lojinha-web")
    # the agent starts after the window's panes are built, so its banner means the layout is ready
    wait_text(panes_of(web)["claude"], "agente simulado da demo: pronto")
    wp = panes_of(web)
    time.sleep(SETTLE_S)
    rec.snap(1.8)                                            # 2. second repo open, its tasks in the panel
    task = "Implemente a tarefa 12 do backlog"
    literal(wp["claude"], task[:14])                         # 3. type the task into the agent
    time.sleep(SETTLE_S / 2)
    rec.snap(0.5)
    literal(wp["claude"], task[14:])
    time.sleep(SETTLE_S / 2)
    rec.snap(0.6)
    send(wp["claude"], "Enter")
    # 4. the agent works: each checkpoint is a named point of the fake run (see fake_claude.py)
    rec.checkpoint(wp["claude"], "Read(src/cart.py)", 0.8, delay=0.5)     # green: reading
    rec.checkpoint(wp["claude"], "Bash(python3", 1.2, delay=10.0)         # yellow: tool running for 10 s
    rec.checkpoint(wp["claude"], "3 passed", 0.8, delay=0.3)              # green again
    rec.checkpoint(wp["claude"], "Pronto: limpar(itens)", 2.0, delay=1.5)  # red: the answer waits for you
    tmux("select-pane", "-R", "-t", wp["claude"])            # S-Right: files tab of the right column
    rec.settle(1.0)
    rec.shot = rec.frames[-1][0]
    tmux("select-pane", "-U", "-t", wp["direita"])           # ctrl+b Up: focus the tab strip
    send(wp["dtabs"], "Right")                               # move to DIFF
    send(wp["dtabs"], "Enter")                               # activate it
    wait_text(wp["direita"], "MUDANÇAS")                     # the diff panel's header
    rec.settle(2.2)                                          # 6. diff of the change
    rec.window = main
    tmux("select-window", "-t", main)                        # prefix+1: back to the first window
    rec.settle(1.6)


def gif_palette(images, grids):
    """One palette for the whole GIF. Every colour the terminal uses (theme and ANSI) is in it exactly,
    so a small red dot is not merged into a neighbouring shade; the rest is median-cut from a sample."""
    theme = []
    for grid in grids:
        for row in grid:
            for cell in row:
                for color in (cell[1], cell[2]):
                    if color not in theme:
                        theme.append(color)
    if len(theme) > 200:
        raise SystemExit(f"{len(theme)} theme colours: too many for one GIF palette")
    room = 256 - len(theme)
    sample = images[:: max(1, len(images) // 6)][:6]
    mosaic = Image.new("RGB", (COLS * CW, ROWS * CH * len(sample)))
    for i, img in enumerate(sample):
        mosaic.paste(img, (0, i * ROWS * CH))
    quantized = mosaic.quantize(colors=room, method=Image.Quantize.MEDIANCUT, dither=Image.Dither.NONE)
    rest = quantized.getpalette()[: room * 3]
    flat = []
    for color in theme:
        flat += [int(color[i:i + 2], 16) for i in (1, 3, 5)]
    flat += rest
    flat += [0] * (768 - len(flat))
    pal = Image.new("P", (1, 1))
    pal.putpalette(flat)
    return pal


def main():
    out = Path(sys.argv[1])
    out.mkdir(parents=True, exist_ok=True)
    rec = Recording()
    scene_script(rec)
    total = sum(h for _, h in rec.frames)
    print(f"frames: {len(rec.frames)} distinct, total {total:.1f} s of GIF time")
    if total > GIF_LIMIT_S:
        raise SystemExit(f"GIF time {total:.1f} s is over the {GIF_LIMIT_S:.0f} s limit")
    renderer = Renderer()
    images = [renderer.draw(g) for g, _ in rec.frames]
    palette = gif_palette(images, [g for g, _ in rec.frames])
    p_frames = [img.quantize(palette=palette, dither=Image.Dither.NONE) for img in images]
    gif = out / "demo.gif"
    p_frames[0].save(gif, save_all=True, append_images=p_frames[1:], loop=0,
                     duration=[int(h * 1000) for _, h in rec.frames], optimize=True, disposal=1)
    shot = out / "image.png"
    renderer.draw(rec.shot).save(shot, optimize=True)
    print(f"{gif}  {os.path.getsize(gif)} bytes  {COLS * CW}x{ROWS * CH}")
    print(f"{shot}  {os.path.getsize(shot)} bytes  {COLS * CW}x{ROWS * CH}")


if __name__ == "__main__":
    main()
