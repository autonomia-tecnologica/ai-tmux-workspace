#!/usr/bin/env python3
"""Tira de abas do Claude (1 linha, acima do painel do Claude): [claude] [tmp 2] ... [+].

Clique numa aba troca, clique no + abre uma sessão temporária. Desenha uma vez e espera o mouse;
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


def estado():
    win = tm("display", "-p", "-t", os.environ.get("TMUX_PANE", ""), "#{window_id}")
    ids = tm("show", "-wqv", "-t", win, "@ws_tabs").split()
    ativo = tm("show", "-wqv", "-t", win, "@ws_ativo")
    abas = []
    for p in ids:
        nome = tm("display", "-p", "-t", p, "#{?pane_id,#{@papel},}")
        if nome:
            abas.append((p, nome))
    return win, abas, ativo


def desenhar(abas, ativo):
    """Devolve (texto, [(inicio, fim, id)]) com colunas 0-based."""
    out, mapa, x = "", [], 0
    for p, nome in abas + [("new", "+")]:
        rot = f" {nome} "
        if p == ativo:
            cor = "\x1b[48;5;75;38;5;16;1m"
        elif p == "new":
            cor = "\x1b[38;5;75;1m"
        else:
            cor = "\x1b[48;5;238;38;5;250m"
        out += f"{cor}{rot}\x1b[0m "
        mapa.append((x, x + len(rot) - 1, p))
        x += len(rot) + 1
    return out, mapa


def main():
    win, abas, ativo = estado()
    texto, mapa = desenhar(abas, ativo)
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    tty.setcbreak(fd)
    sys.stdout.write("\x1b[?25l\x1b[2J\x1b[H" + texto + "\x1b[?1000h\x1b[?1006h")
    sys.stdout.flush()
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
                            cmd = [WS, "tab-new", win] if p == "new" else [WS, "tab-show", win, p]
                            subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                            break
            buf = buf[-64:]
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)
        sys.stdout.write("\x1b[?1006l\x1b[?1000l\x1b[?25h")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
