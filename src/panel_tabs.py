#!/usr/bin/env python3
"""Barra superior de cada painel (1 linha; arg: claude|terminal|repos|tasks|side):
[CLAUDE] [CLAUDE (2)] [CLAUDE (3)] ... [+]. Em `repos` são REPOS e os plugins listados (sem +); em `tasks` é só o título do painel (uma aba, sem +).
No modo `side` as abas são fixas (ARQUIVOS, SESSÕES, DIFF, GITHUB) e escolhem o que o painel da direita mostra.

Clique numa aba troca, clique no + abre uma sessão temporária. Com o painel focado, ←/→ movem o
cursor (fundo mais claro) entre as abas e o +, e Enter ativa. Desenha e espera mouse/teclado;
o `workspace` reinicia este painel (respawn-pane) a cada mudança de abas.
"""
import os
import re
import select
import signal
import subprocess
import sys
import termios
import tty

import i18n
import lib
import tmux
from colors import R, TAB_ACTIVE, TAB_CURSOR, TAB_IDLE, TAB_NEW
from viewer import direcao

SGR = re.compile(rb"\x1b\[<(\d+);(\d+);(\d+)([Mm])")


KIND = sys.argv[1] if len(sys.argv) > 1 and sys.argv[1] in ("terminal", "side", "repos", "tasks") else "claude"
NO_PLUS = ("side", "repos", "tasks")
PAD = 1 if KIND in ("claude", "repos", "side") else 0   # top-row bars get a blank line above, off the status bar   # top-row bars get a blank line above, off the status bar


def t(key):
    return i18n.t(f"tabs.{key}")


SIDE_TABS = [(k, t(k)) for k in ("tree", "diff", "github")]
VARS = {"claude": ("@ws_tabs", "@ws_ativo"), "terminal": ("@ws_ttabs", "@ws_tativo")}


def sessao():
    return tmux.run("display", "-p", "-t", os.environ.get("TMUX_PANE", ""), "#{session_name}")


def abas_plugins(perfil):
    """[(\"plugin:<id>\", rótulo)] for each valid plugin listed in config.yaml (broken ones have no window)."""
    abas = []
    for entrada in lib.plugins_listados(perfil):
        try:
            id_, _, rotulo, _ = lib.carregar_plugin(entrada)
        except lib.ErroPlugin:
            continue
        abas.append((f"plugin:{id_}", rotulo))
    return abas


def janela_plugin(chave):
    """Window id of the plugin window named like the tab key (plugin windows are named plugin:<id>)."""
    for linha in tmux.run("list-windows", "-t", sessao(), "-F", "#{window_id}|#{window_name}").splitlines():
        wid, _, nome = linha.partition("|")
        if nome == chave:
            return wid
    return ""


def janela_repo():
    """Window to go back to from a plugin window: the previous window (tmux's own history) when it is a
    repo, else the first repo window of the session. Empty when the session has no repo window."""
    sess = sessao()
    anterior = tmux.run("display", "-p", "-t", f"{sess}:{{last}}", "#{window_id}")
    if anterior and tmux.run("show", "-wqv", "-t", anterior, "@ws_repo"):
        return anterior
    for linha in tmux.run("list-windows", "-t", sess, "-F", "#{window_id}|#{@ws_repo}").splitlines():
        wid, _, repo = linha.partition("|")
        if repo:
            return wid
    return ""


def estado():
    win =tmux.run("display", "-p", "-t", os.environ.get("TMUX_PANE", ""), "#{window_id}")
    if KIND == "side":
        modo = tmux.run("show", "-wqv", "-t", win, "@ws_side")
        return win, SIDE_TABS, modo if modo in ("diff", "github") else "tree"
    if KIND == "repos":
        # REPOS plus the listed plugins; the active one is the window the panel runs in
        nome = tmux.run("display", "-p", "-t", win, "#{window_name}")
        modo = tmux.run("show", "-wqv", "-t", win, "@ws_left")
        ativo = nome if nome.startswith("plugin:") else ("sessions" if modo == "sessions" else "repos")
        return win, [("repos", t("repos")), ("sessions", t("sessions"))] + abas_plugins(sessao()), ativo
    if KIND == "tasks":
        return win, [(KIND, t(KIND))], KIND
    v_tabs, v_ativo = VARS[KIND]
    ids = tmux.run("show", "-wqv", "-t", win, v_tabs).split()
    ativo = tmux.run("show", "-wqv", "-t", win, v_ativo)
    abas = []
    for p in ids:
        if tmux.run("display", "-p", "-t", p, "#{?pane_id,#{@papel},}"):
            # the main tab has no number; the others are numbered by creation order (2, 3, ...)
            abas.append((p, t(KIND) if not abas else f"{t(KIND)} ({len(abas) + 1})"))
    return win, abas, ativo


def desenhar(abas, ativo, cursor=None):
    """Devolve (texto, [(inicio, fim, id)]) com colunas 0-based."""
    out, mapa, x = "", [], 0
    for p, nome in abas + ([] if KIND in NO_PLUS else [("new", "+")]):
        rot = f" {nome} "
        if p == ativo:
            cor = TAB_ACTIVE
        elif p == "new":
            cor = TAB_NEW
        else:
            cor = TAB_IDLE
        if p == cursor and p != ativo:   # keyboard cursor: lighter background (no underline)
            cor = TAB_CURSOR
        out += f"{cor}{rot}{R} "
        mapa.append((x, x + len(rot) - 1, p))
        x += len(rot) + 1
    return out.rstrip(" "), mapa   # no trailing space: on a full-width bar it would wrap and scroll the bar up


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
        if KIND == "repos" and p.startswith("plugin:"):
            wid = janela_plugin(p)
            if wid:
                tmux.run("select-window", "-t", wid)
            return
        if KIND == "repos" and p in ("repos", "sessions") and tmux.run("display", "-p", "-t", win, "#{window_name}").startswith("plugin:"):
            wid = janela_repo()
            if wid:
                tmux.run("select-window", "-t", wid)
                subprocess.Popen([lib.WS, "left", p, wid], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return
        if KIND == "repos" and p in ("repos", "sessions"):
            subprocess.Popen([lib.WS, "left", p, win], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return
        if KIND in ("repos", "tasks"):
            return
        if KIND == "side":
            cmd = [lib.WS, "side", p, win]
        else:
            cmd = [lib.WS, "tab-new", win, KIND] if p == "new" else [lib.WS, "tab-show", win, p]
        subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    cursor = [cur]

    def redraw(*_):   # resize (fit_window) can scroll the bar up: clear and draw again on its row
        sys.stdout.write("\x1b[2J" + f"\x1b[{PAD + 1};1H" + desenhar(abas, ativo, itens[cursor[0]])[0])
        sys.stdout.flush()

    signal.signal(signal.SIGWINCH, redraw)
    try:
        buf = b""
        while True:
            try:
                if not select.select([fd], [], [], 60)[0]:
                    continue
                buf += os.read(fd, 4096)
            except InterruptedError:
                continue
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
                d = direcao(k.decode("latin1"))
                if d == "left":
                    passo = -1
                elif d == "right":
                    passo = 1
                else:
                    abrir(itens[cur])
                    continue
                cur = max(0, min(len(itens) - 1, cur + passo))
                cursor[0] = cur
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
