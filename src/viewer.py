"""Visualizador de terminal sem flicker: reescreve só as linhas que mudaram.

Nada de clear/refresh de tela cheia (é isso que deixa "artefato" de caractere). Cada painel
fornece `coletar(viewer) -> (cabecalho, corpo)` — rodado numa thread, de tempos em tempos —
e o laço principal só desenha, rola e trata teclado/mouse.

Menu de contexto: com `acoes(viewer, chave) -> [(rotulo, ativa, funcao)]`, Espaço ou clique direito
sobre um item abre um overlay; setas/j/k navegam, Enter executa `funcao(viewer, chave)`, Esc fecha.
Lista vazia não abre menu. Item inativo aparece apagado e Enter nele só fecha.
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
MENU_BG = "\x1b[48;5;239m"
FOOTER_ROWS = 3
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


def _largura(s):
    """Colunas visíveis de `s` (ignora ANSI)."""
    return sum(_larg(c) for c in ANSI.sub("", s))


class Linha:
    __slots__ = ("texto", "chave", "cont")

    def __init__(self, texto="", chave=None, cont=False):
        # cont: continuation of the previous keyed line; not navigable, but highlighted with it
        self.texto, self.chave, self.cont = texto, chave, cont


class Viewer:
    def __init__(self, coletar, intervalo=2.0, cursor=False, ao_enter=None, chave_inicial=None,
                 ao_tecla=None, ao_perde_foco=None, rodape=None, acoes=None):
        self.coletar, self.intervalo, self.cursor = coletar, intervalo, cursor
        self.ao_enter, self.ao_tecla, self.ao_perde_foco = ao_enter, ao_tecla, ao_perde_foco
        self.rodape = rodape    # footer(viewer) -> full text of the selection, wrapped on a fixed footer
        self.acoes = acoes      # acoes(viewer, chave) -> [(rotulo, ativa, funcao(viewer, chave))]
        self.cab, self.corpo, self._sig = [], [], None
        self.top, self.chave, self.idx = 0, chave_inicial, 0
        self.recoletar = threading.Event()
        self.parar = False
        self._lock = threading.Lock()
        self._rp, self._wp = os.pipe()
        self._frame = None
        self._tam = None
        self.menu = None        # {"chave", "itens", "sel", "linha", "coluna"} while the context menu is open
        self._caixa = None      # (x, y, largura, altura) of the menu box as last drawn

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
        h = max(0, lins - len(cab) - self._rod_h())
        self.top = max(0, min(self.top, max(0, len(corpo) - h)))
        end = self.idx + 1
        while self.cursor and end < len(corpo) and corpo[end].cont:
            end += 1
        rows = []
        for l in cab[:lins]:
            rows.append(cortar(l.texto, cols)[0])
        n_cab = len(rows)
        for r in range(h):
            i = self.top + r
            if i >= len(corpo):
                rows.append("")
                continue
            t, n = cortar(corpo[i].texto, cols)
            if self.cursor and self.idx <= i < end and (i == self.idx and corpo[i].chave is not None or corpo[i].cont):
                t = BG + t.replace(R, R + BG) + " " * (cols - n) + R
            rows.append(t)
        self._caixa = None
        if self.menu is not None:
            self._sobrepor_menu(rows, n_cab, h, cols)
        if self.rodape and lins >= len(rows) + FOOTER_ROWS:
            rows = self._com_rodape(rows, cols, lins)
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

    def _sobrepor_menu(self, rows, n_cab, h, cols):
        """Draws the context menu as a box over the body, below the row it was opened from."""
        itens = self.menu["itens"]
        alt = len(itens)
        if h < alt:
            return
        w = max(_largura(rot) for rot, _, _ in itens) + 2
        y0 = max(n_cab, min(self.menu["linha"] + 1, n_cab + h - alt))
        x0 = max(0, min(self.menu["coluna"], cols - w))
        for j, (rot, ativa, _) in enumerate(itens):
            if j == self.menu["sel"]:
                estilo = MENU_BG + B + REV
            else:
                estilo = MENU_BG + ("" if ativa else DIM)
            esquerda, nl = cortar(rows[y0 + j], x0)
            esquerda += " " * (x0 - nl)
            texto = " " + rot + " " * (w - 1 - _largura(rot))
            rows[y0 + j] = esquerda + R + estilo + texto + R
        self._caixa = (x0, y0, w, alt)

    def _rod_h(self):
        return FOOTER_ROWS if self.rodape else 0

    def _com_rodape(self, rows, cols, lins):
        """Pads the body up to the footer area, then draws a separator and the selection wrapped in
        the last FOOTER_ROWS - 1 rows (the tail is cut with an ellipsis when it still does not fit)."""
        rows = rows[:lins - FOOTER_ROWS]
        while len(rows) < lins - FOOTER_ROWS:
            rows.append("")
        try:
            texto = self.rodape(self) or ""
        except Exception:
            texto = ""
        linhas, w = [], max(1, cols - 1)
        while texto and len(linhas) < FOOTER_ROWS - 1:
            parte, n = cortar(texto, w)
            if len(parte) >= len(texto):
                linhas.append(parte)
                break
            linhas.append(parte)
            texto = texto[len(parte):]
        else:
            if texto:
                linhas[-1] = cortar(linhas[-1], max(1, w - 1))[0] + "…"
        rows.append(f"{CINZA}{'─' * cols}{R}")
        rows += [f"{DIM}{l}{R}" for l in linhas]
        return rows

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
        h = max(1, (self._tam or (80, 24))[1] - len(self.cab) - self._rod_h())
        self._visivel(h)
        self.recoletar.set()    # painéis que reagem à seleção (ex.: tarefas) redesenham já

    def _rolar(self, d):
        h = max(1, (self._tam or (80, 24))[1] - len(self.cab) - self._rod_h())
        self.top = max(0, min(self.top + d, max(0, len(self.corpo) - h)))

    def _abrir_menu(self, linha, coluna):
        """Opens the context menu for the selected item (linha: screen row, 0-based). No actions, no menu."""
        if not self.acoes or not (0 <= self.idx < len(self.corpo)) or self.corpo[self.idx].chave is None:
            return
        chave = self.corpo[self.idx].chave
        itens = self.acoes(self, chave)
        if itens:
            self.menu = {"chave": chave, "itens": itens, "sel": 0, "linha": linha, "coluna": coluna}

    def _executar_menu(self, j):
        m = self.menu
        self.menu = None
        _, ativa, funcao = m["itens"][j]
        if ativa:
            funcao(self, m["chave"])
            self.recoletar.set()

    def _tecla_menu(self, k):
        m = self.menu
        n = len(m["itens"])
        if k in ("j", "\x1b[B", "\x1bOB"):
            m["sel"] = (m["sel"] + 1) % n
        elif k in ("k", "\x1b[A", "\x1bOA"):
            m["sel"] = (m["sel"] - 1) % n
        elif k in ("\r", "\n"):
            self._executar_menu(m["sel"])
        elif k == "esc":
            self.menu = None

    def _tecla(self, k):
        if self.menu is not None:
            self._tecla_menu(k)
            return
        h = max(1, (self._tam or (80, 24))[1] - len(self.cab) - self._rod_h())
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
        elif k in ("\r", "\n", "l") + (() if self.acoes else (" ",)) and self.cursor and self.ao_enter:
            if 0 <= self.idx < len(self.corpo) and self.corpo[self.idx].chave is not None:
                self.ao_enter(self, self.corpo[self.idx].chave, k)
        elif k == " " and self.cursor:
            self._abrir_menu(len(self.cab) + self.idx - self.top, 2)
        elif k == "r":
            self.recoletar.set()
        elif k == "\x1b[O":     # focus lost (tmux focus-events)
            if self.ao_perde_foco:
                self.ao_perde_foco(self)
        elif k == "\x1b[I":
            pass
        elif self.ao_tecla:
            self.ao_tecla(self, k)

    def _indice_em(self, linha):
        """Index in the body of the selectable row at screen row `linha` (0-based), or None."""
        h = max(0, (self._tam or (80, 24))[1] - len(self.cab) - self._rod_h())
        if linha < len(self.cab) or linha - len(self.cab) >= h:
            return None
        i = self.top + (linha - len(self.cab))
        if 0 <= i < len(self.corpo) and self.corpo[i].chave is not None:
            return i
        return None

    def _mouse(self, b, x, y, press):
        linha, coluna = y - 1, x - 1
        if b == 64:
            self._rolar(-3)
        elif b == 65:
            self._rolar(3)
        elif press and b in (0, 2) and self.menu is not None:
            if b == 0 and self._caixa:
                cx, cy, cw, ca = self._caixa
                if cx <= coluna < cx + cw and cy <= linha < cy + ca:
                    self._executar_menu(linha - cy)
                    return
            self.menu = None        # click outside the box closes the menu
        elif b == 0 and press and self.cursor and self.ao_enter:
            i = self._indice_em(linha)
            if i is not None:
                self.idx, self.chave = i, self.corpo[i].chave
                self.recoletar.set()
                self.ao_enter(self, self.chave, "click")
        elif b == 2 and press and self.cursor and self.acoes:
            i = self._indice_em(linha)
            if i is not None:
                self.idx, self.chave = i, self.corpo[i].chave
                self.recoletar.set()
                self._abrir_menu(linha, coluna)

    def _processar(self, dados):
        i = 0
        while i < len(dados):
            m = SGR.match(dados, i)
            if m:
                self._mouse(int(m.group(1)), int(m.group(2)), int(m.group(3)), m.group(4) == b"M")
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
