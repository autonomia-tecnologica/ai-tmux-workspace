"""Visualizador de terminal sem flicker: reescreve só as linhas que mudaram.

Nada de clear/refresh de tela cheia (é isso que deixa "artefato" de caractere). Cada painel
fornece `coletar(viewer) -> (cabecalho, corpo)` — rodado numa thread, de tempos em tempos —
e o laço principal só desenha, rola e trata teclado/mouse.
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

ANSI = re.compile(r"\x1b\[[0-9;:?<=>]*[ -/]*[@-~]")
SGR = re.compile(rb"\x1b\[<(\d+);(\d+);(\d+)([Mm])")
BG = "\x1b[48;5;237m"
R, B, DIM, REV = "\x1b[0m", "\x1b[1m", "\x1b[2m", "\x1b[7m"
VERDE, AMARELO, VERMELHO, AZUL, CINZA = ("\x1b[32m", "\x1b[33m", "\x1b[31m", "\x1b[38;5;75m",
                                          "\x1b[38;5;244m")


def _larg(c):
    if unicodedata.combining(c):
        return 0
    return 2 if unicodedata.east_asian_width(c) in "WF" else 1


def cortar(s, w):
    """Corta `s` em `w` colunas visíveis preservando as sequências ANSI. -> (texto, largura)."""
    out, n, i = [], 0, 0
    while i < len(s):
        m = ANSI.match(s, i)
        if m:
            out.append(m.group())
            i = m.end()
            continue
        k = _larg(s[i])
        if n + k > w:
            break
        out.append(s[i])
        n += k
        i += 1
    return "".join(out), n


class Linha:
    __slots__ = ("texto", "chave", "cont")

    def __init__(self, texto="", chave=None, cont=False):
        # cont: continuation of the previous keyed line; not navigable, but highlighted with it
        self.texto, self.chave, self.cont = texto, chave, cont


class Viewer:
    def __init__(self, coletar, intervalo=2.0, cursor=False, ao_enter=None, chave_inicial=None,
                 ao_tecla=None, ao_perde_foco=None):
        self.coletar, self.intervalo, self.cursor = coletar, intervalo, cursor
        self.ao_enter, self.ao_tecla, self.ao_perde_foco = ao_enter, ao_tecla, ao_perde_foco
        self.cab, self.corpo, self._sig = [], [], None
        self.top, self.chave, self.idx = 0, chave_inicial, 0
        self.recoletar = threading.Event()
        self.parar = False
        self._lock = threading.Lock()
        self._rp, self._wp = os.pipe()
        self._frame = None
        self._tam = None

    # ---- coleta (thread) -------------------------------------------------------------
    def _coletor(self):
        while not self.parar:
            try:
                cab, corpo = self.coletar(self)
            except Exception as e:      # um erro de coleta não derruba o painel
                cab, corpo = [], [Linha(" " + i18n.t("viewer.error", e=e))]
            sig = (tuple((l.texto, l.chave) for l in cab), tuple((l.texto, l.chave) for l in corpo))
            with self._lock:
                if sig != self._sig:
                    self._sig, self.cab, self.corpo = sig, cab, corpo
                    self._acordar()
            self.recoletar.wait(self.intervalo)
            self.recoletar.clear()

    def _acordar(self):
        try:
            os.write(self._wp, b"x")
        except OSError:
            pass

    # ---- desenho ---------------------------------------------------------------------
    def _localizar_cursor(self):
        if not self.cursor:
            return
        sel = [i for i, l in enumerate(self.corpo) if l.chave is not None]
        if not sel:
            self.idx = 0
            return
        for i in sel:
            if self.corpo[i].chave == self.chave:
                self.idx = i
                return
        self.idx = min(sel, key=lambda i: abs(i - self.idx))
        self.chave = self.corpo[self.idx].chave

    def _visivel(self, h):
        if self.idx < self.top:
            self.top = self.idx
        elif self.idx >= self.top + h:
            self.top = self.idx - h + 1

    def _desenhar(self, forcar=False):
        cols, lins = shutil.get_terminal_size((80, 24))
        with self._lock:
            cab, corpo = self.cab, self.corpo
        self._localizar_cursor()
        h = max(0, lins - len(cab))
        self.top = max(0, min(self.top, max(0, len(corpo) - h)))
        end = self.idx + 1
        while self.cursor and end < len(corpo) and corpo[end].cont:
            end += 1
        rows = []
        for l in cab[:lins]:
            rows.append(cortar(l.texto, cols)[0])
        for r in range(h):
            i = self.top + r
            if i >= len(corpo):
                rows.append("")
                continue
            t, n = cortar(corpo[i].texto, cols)
            if self.cursor and self.idx <= i < end and (i == self.idx and corpo[i].chave is not None or corpo[i].cont):
                t = BG + t.replace(R, R + BG) + " " * (cols - n) + R
            rows.append(t)
        while len(rows) < lins:
            rows.append("")
        if forcar or self._frame is None or self._tam != (cols, lins):
            self._frame, self._tam = [None] * lins, (cols, lins)
        buf = []
        for y, t in enumerate(rows):
            if self._frame[y] != t:
                buf.append(f"\x1b[{y + 1};1H{t}{R}\x1b[K")
                self._frame[y] = t
        if buf:
            sys.stdout.write("\x1b[?2026h" + "".join(buf) + "\x1b[?2026l")
            sys.stdout.flush()

    # ---- entrada ---------------------------------------------------------------------
    def _mover(self, d):
        sel = [i for i, l in enumerate(self.corpo) if l.chave is not None]
        if not sel:
            return
        if d in ("ini", "fim"):
            self.idx = sel[0] if d == "ini" else sel[-1]
        else:
            pos = min(range(len(sel)), key=lambda k: abs(sel[k] - self.idx))
            self.idx = sel[max(0, min(len(sel) - 1, pos + d))]
        self.chave = self.corpo[self.idx].chave
        h = max(1, (self._tam or (80, 24))[1] - len(self.cab))
        self._visivel(h)
        self.recoletar.set()    # painéis que reagem à seleção (ex.: tarefas) redesenham já

    def _rolar(self, d):
        h = max(1, (self._tam or (80, 24))[1] - len(self.cab))
        self.top = max(0, min(self.top + d, max(0, len(self.corpo) - h)))

    def _tecla(self, k):
        h = max(1, (self._tam or (80, 24))[1] - len(self.cab))
        if k in ("j", "\x1b[B", "\x1bOB"):
            self._mover(1) if self.cursor else self._rolar(1)
        elif k in ("k", "\x1b[A", "\x1bOA"):
            self._mover(-1) if self.cursor else self._rolar(-1)
        elif k in ("\x1b[6~", "\x04"):
            self._mover(h // 2) if self.cursor else self._rolar(h - 1)
        elif k in ("\x1b[5~", "\x15"):
            self._mover(-(h // 2)) if self.cursor else self._rolar(-(h - 1))
        elif k in ("g", "\x1b[H", "\x1b[1~"):
            self._mover("ini") if self.cursor else setattr(self, "top", 0)
        elif k in ("G", "\x1b[F", "\x1b[4~"):
            self._mover("fim") if self.cursor else self._rolar(10 ** 6)
        elif k in ("\r", "\n", "l", " ") and self.cursor and self.ao_enter:
            if 0 <= self.idx < len(self.corpo) and self.corpo[self.idx].chave is not None:
                self.ao_enter(self, self.corpo[self.idx].chave, k)
        elif k == "r":
            self.recoletar.set()
        elif k == "\x1b[O":     # focus lost (tmux focus-events)
            if self.ao_perde_foco:
                self.ao_perde_foco(self)
        elif k == "\x1b[I":
            pass
        elif self.ao_tecla:
            self.ao_tecla(self, k)

    def _mouse(self, b, y, press):
        if b == 64:
            self._rolar(-3)
        elif b == 65:
            self._rolar(3)
        elif b == 0 and press and self.cursor and self.ao_enter:
            i = self.top + (y - 1 - len(self.cab))
            if 0 <= i < len(self.corpo) and self.corpo[i].chave is not None:
                self.idx, self.chave = i, self.corpo[i].chave
                self.recoletar.set()
                self.ao_enter(self, self.chave, "click")

    def _processar(self, dados):
        i = 0
        while i < len(dados):
            m = SGR.match(dados, i)
            if m:
                self._mouse(int(m.group(1)), int(m.group(3)), m.group(4) == b"M")
                i = m.end()
                continue
            if dados[i:i + 1] == b"\x1b":
                m = re.compile(rb"\x1b(\[[0-9;]*[~A-Za-z]|O[A-Za-z])").match(dados, i)
                if m:
                    self._tecla(m.group().decode("latin1"))
                    i = m.end()
                    continue
                self._tecla("esc")      # lone Esc key
                i += 1
                continue
            j = i + 1
            while j < len(dados) and dados[j] & 0xC0 == 0x80:
                j += 1
            self._tecla(dados[i:j].decode("utf-8", "replace"))
            i = j

    # ---- laço ------------------------------------------------------------------------
    def rodar(self):
        fd = sys.stdin.fileno()
        antigo = termios.tcgetattr(fd)
        signal.signal(signal.SIGWINCH, lambda *_: self._acordar())
        signal.signal(signal.SIGTERM, lambda *_: setattr(self, "parar", True) or self._acordar())
        tty.setcbreak(fd)
        sys.stdout.write("\x1b[?1049h\x1b[?25l\x1b[?1000h\x1b[?1006h\x1b[?1004h")
        sys.stdout.flush()
        threading.Thread(target=self._coletor, daemon=True).start()
        try:
            while not self.parar:
                self._desenhar()
                pronto, _, _ = select.select([fd, self._rp], [], [], 5)
                if self._rp in pronto:
                    os.read(self._rp, 4096)
                if fd in pronto:
                    d = os.read(fd, 4096)
                    if not d:
                        break
                    self._processar(d)
        finally:
            self.parar = True
            sys.stdout.write("\x1b[?1004l\x1b[?1006l\x1b[?1000l\x1b[?25h\x1b[?1049l")
            sys.stdout.flush()
            termios.tcsetattr(fd, termios.TCSADRAIN, antigo)
