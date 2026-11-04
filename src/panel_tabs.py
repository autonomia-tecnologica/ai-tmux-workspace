#!/usr/bin/env python3
"""Barra superior de cada painel (1 linha; arg: claude|terminal|repos|tasks|side):
[CLAUDE] [CLAUDE (2)] [CLAUDE (3)] ... [+]. Em `repos` e `tasks` é só o título do painel (uma aba, sem +).
No modo `side` as abas são fixas (ARQUIVOS, SESSÕES, DIFF, GITHUB) e escolhem o que o painel da direita mostra.

Clique numa aba troca, clique no + abre uma sessão temporária. Com o painel focado, ←/→ movem o
cursor (fundo mais claro) entre as abas e o +, e Enter ativa. Desenha e espera mouse/teclado;
o `workspace` reinicia este painel (respawn-pane) a cada mudança de abas.
"""
import os
import re
import select
import subprocess
import sys
import termios
import tty

WS = os.path.join(os.environ.get("WS_DIR", os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
                  "bin", "workspace")
SGR = re.compile(rb"\x1b\[<(\d+);(\d+);(\d+)([Mm])")


def tm(*args):
    return subprocess.run(["tmux", *args], capture_output=True, text=True).stdout.strip()


KIND = sys.argv[1] if len(sys.argv) > 1 and sys.argv[1] in ("terminal", "side", "repos", "tasks") else "claude"
NO_PLUS = ("side", "repos", "tasks")
PAD = 1 if KIND in ("claude", "repos", "side") else 0   # top-row bars get a blank line above, off the status bar   # top-row bars get a blank line above, off the status bar
STRINGS = {
    "en-US": {"claude": "CLAUDE", "terminal": "TERMINAL", "repos": "REPOS", "tasks": "TASKS",
              "tree": "FILES", "sessions": "SESSIONS", "diff": "DIFF", "github": "GITHUB"},
    "pt-BR": {"claude": "CLAUDE", "terminal": "TERMINAL", "repos": "REPOS", "tasks": "TAREFAS",
              "tree": "ARQUIVOS", "sessions": "SESSÕES", "diff": "DIFF", "github": "GITHUB"},
}
LOCALE = "pt-BR" if os.environ.get("LC_ALL", os.environ.get("LANG", "")).startswith("pt") else "en-US"


def t(key):
    return STRINGS[LOCALE][key]


SIDE_TABS = [(k, t(k)) for k in ("tree", "sessions", "diff", "github")]
VARS = {"claude": ("@ws_tabs", "@ws_ativo"), "terminal": ("@ws_ttabs", "@ws_tativo")}


def estado():
    win = tm("display", "-p", "-t", os.environ.get("TMUX_PANE", ""), "#{window_id}")
    if KIND == "side":
        return win, SIDE_TABS, tm("show", "-wqv", "-t", win, "@ws_side") or "tree"
    if KIND in ("repos", "tasks"):
        return win, [(KIND, t(KIND))], KIND
    v_tabs, v_ativo = VARS[KIND]
    ids = tm("show", "-wqv", "-t", win, v_tabs).split()
    ativo = tm("show", "-wqv", "-t", win, v_ativo)
    abas = []
    for p in ids:
        if tm("display", "-p", "-t", p, "#{?pane_id,#{@papel},}"):
            # the main tab has no number; the others are numbered by creation order (2, 3, ...)
            abas.append((p, t(KIND) if not abas else f"{t(KIND)} ({len(abas) + 1})"))
    return win, abas, ativo


def desenhar(abas, ativo, cursor=None):
    """Devolve (texto, [(inicio, fim, id)]) com colunas 0-based."""
    out, mapa, x = "", [], 0
    for p, nome in abas + ([] if KIND in NO_PLUS else [("new", "+")]):
        rot = f" {nome} "
        if p == ativo:
            cor = "\x1b[48;5;75;38;5;16;1m"
        elif p == "new":
            cor = "\x1b[38;5;75;1m"
        else:
            cor = "\x1b[48;5;238;38;5;250m"
        if p == cursor and p != ativo:   # keyboard cursor: lighter background (no underline)
            cor = "\x1b[48;5;250;38;5;16m"
        out += f"{cor}{rot}\x1b[0m "
        mapa.append((x, x + len(rot) - 1, p))
        x += len(rot) + 1
    return out, mapa


def main():
    win, abas, ativo = estado()
    itens = [p for p, _ in abas] + ([] if KIND in NO_PLUS else ["new"])
    cur = itens.index(ativo) if ativo in itens else 0
    texto, mapa = desenhar(abas, ativo, itens[cur])
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    tty.setcbreak(fd)
    sys.stdout.write("\x1b[?25l\x1b[2J" + f"\x1b[{PAD + 1};1H" + texto + "\x1b[?1000h\x1b[?1006h")
    sys.stdout.flush()

    def abrir(p):
        if KIND in ("repos", "tasks"):
            return
        if KIND == "side":
            cmd = [WS, "side", p, win]
        else:
            cmd = [WS, "tab-new", win, KIND] if p == "new" else [WS, "tab-show", win, p]
        subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    try:
        buf = b""
        while True:
            if not select.select([fd], [], [], 60)[0]:
                continue
            buf += os.read(fd, 4096)
            for m in SGR.finditer(buf):
                b, x, press = int(m.group(1)), int(m.group(2)) - 1, m.group(4) == b"M"
                if b == 0 and press:
                    for ini, fim, p in mapa:
                        if ini <= x <= fim:
                            abrir(p)
                            break
            buf = SGR.sub(b"", buf)
            passo = 0
            for k in re.findall(rb"\x1b[\[O][CD]|\r|\n|[hl]", buf):
                if k in (b"\x1b[D", b"\x1bOD", b"h"):
                    passo = -1
                elif k in (b"\x1b[C", b"\x1bOC", b"l"):
                    passo = 1
                else:
                    abrir(itens[cur])
                    continue
                cur = max(0, min(len(itens) - 1, cur + passo))
                sys.stdout.write(f"\x1b[{PAD + 1};1H" + desenhar(abas, ativo, itens[cur])[0])
                sys.stdout.flush()
            buf = b"" if len(buf) < 64 else buf[-8:]
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)
        sys.stdout.write("\x1b[?1006l\x1b[?1000l\x1b[?25h")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
