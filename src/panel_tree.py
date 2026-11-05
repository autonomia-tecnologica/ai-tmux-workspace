#!/usr/bin/env python3
"""Árvore de arquivos do repo da janela. Enter abre/fecha pasta ou abre o arquivo no editor."""
import os
import shlex
import shutil
import subprocess

import clipboard
import icons
import lib
from i18n import t
import opener
from viewer import AMARELO, AZUL, B, CINZA, DIM, R, VERDE, Linha, Viewer

REPO = lib.repo_atual()
IGNORA = {".git", "node_modules", "__pycache__", ".venv", "target", ".mypy_cache", ".pytest_cache"}
ABERTAS = {REPO}
LIMITE = 400
USE_ICONS = icons.enabled()


def status_git():
    rc, out = lib.git(REPO, "status", "--porcelain", "-uall")
    res = {}
    if rc == 0:
        for l in out.splitlines():
            res[os.path.join(REPO, l[3:].split(" -> ")[-1].strip('"'))] = l[:2]
    return res


def listar(pasta):
    try:
        itens = [e for e in os.scandir(pasta) if e.name not in IGNORA]
    except OSError:
        return []
    itens.sort(key=lambda e: (not e.is_dir(follow_symlinks=False), e.name.lower()))
    return itens[:LIMITE]


_last_cwd = [None]


def _tmux(*args):
    try:
        r = subprocess.run(["tmux", *args], capture_output=True, text=True, timeout=3)
        return r.stdout.strip()
    except Exception:
        return ""


def follow_terminal(v):
    """When the shell in the active terminal tab changes directory, expand that folder (only it,
    not its children) and move the selection there. Only reacts to a change, so manual navigation
    in the tree is not overridden."""
    pane = os.environ.get("TMUX_PANE")
    if not pane:
        return
    win = _tmux("display", "-p", "-t", pane, "#{window_id}")
    target = _tmux("show", "-wqv", "-t", win, "@ws_tativo")
    if not target:
        target = _tmux("list-panes", "-t", win, "-F", "#{pane_id}|#{@papel}")
        target = next((l.split("|")[0] for l in target.splitlines() if l.endswith("|terminal")), "")
    if not target:
        return
    cwd = os.path.realpath(_tmux("display", "-p", "-t", target, "#{pane_current_path}") or REPO)
    if cwd == _last_cwd[0]:
        return
    _last_cwd[0] = cwd
    repo = os.path.realpath(REPO)
    if cwd == repo:                 # the root is not a row: select the first entry and scroll to the top
        first = next(iter(listar(REPO)), None)
        if first:
            v.chave = ("d" if first.is_dir(follow_symlinks=False) else "f", first.path)
            v.top = 0
            v._acordar()
        return
    if not cwd.startswith(repo + "/"):
        return
    rel = os.path.relpath(cwd, repo).split(os.sep)
    for i in range(len(rel)):       # open the ancestors and the folder itself (its subfolders stay closed)
        ABERTAS.add(os.path.join(REPO, *rel[:i + 1]))
    v.chave = ("d", os.path.join(REPO, *rel))
    v._acordar()


def coletar(v):
    follow_terminal(v)
    st = status_git()
    sujos = {os.path.dirname(p) for p in st}
    for p in list(sujos):
        while p.startswith(REPO) and p != REPO:
            p = os.path.dirname(p)
            sujos.add(p)
    cab = []
    corpo = []

    def descer(pasta, nivel):
        for e in listar(pasta):
            ind = "  " * nivel
            if e.is_dir(follow_symlinks=False):
                aberta = e.path in ABERTAS
                mark = f" {AMARELO}•{R}" if e.path in sujos else ""
                ic = icons.dir_icon(aberta) if USE_ICONS else ""
                corpo.append(Linha(f" {ind}{CINZA}{'▾' if aberta else '▸'}{R} {ic}{AZUL}{e.name}/{R}{mark}",
                                   ("d", e.path)))
                if aberta:
                    descer(e.path, nivel + 1)
            else:
                s = st.get(e.path)
                cor = "" if not s else (VERDE if s.strip() in ("??", "A") else AMARELO)
                ic = icons.file_icon(e.name) if USE_ICONS else ""
                corpo.append(Linha(f" {ind}  {ic}{cor}{e.name}{R}", ("f", e.path)))

    descer(REPO, 0)
    return cab, corpo


def footer(v):
    """Full name of the selected entry (the row itself is cut at the pane width)."""
    if not v.chave or v.chave[1] == REPO:
        return ""
    return os.path.relpath(v.chave[1], REPO) + ("/" if v.chave[0] == "d" else "")


def editor():
    for c in ("nvim", "vim", "vi"):
        if c and shutil.which(c.split()[0]):
            return c
    return "vi"


def enter(v, chave, tecla):
    tipo, caminho = chave
    if tipo == "d":
        ABERTAS.symmetric_difference_update({caminho})
        v.recoletar.set()
    elif opener.use_system(caminho) and opener.open_system(caminho):
        return
    else:
        subprocess.run(["tmux", "display-popup", "-E", "-w", "92%", "-h", "92%", "-d", REPO,
                        f"{editor()} {shlex.quote(caminho)}"],
                       timeout=None)


def _copied(path):
    try:
        return clipboard.copy_file(path)
    except Exception:
        return None


def copy_selected(v):
    """`y`: puts the selected file on the clipboard as a file, to paste elsewhere."""
    if not v.chave or v.chave[0] != "f":
        msg = t("tree.copy_pick_file")
    elif _copied(v.chave[1]):
        msg = t("tree.copied", name=os.path.basename(v.chave[1]))
    else:
        msg = t("tree.copy_failed")
    subprocess.run(["tmux", "display-message", "-d", "2000", msg], check=False, stdin=subprocess.DEVNULL,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def on_key(v, k):
    """Right opens the folder under the cursor, Left closes it (or jumps to the parent folder). `y` copies the file."""
    if k == "y":
        copy_selected(v)
        return
    if k not in ("\x1b[C", "\x1bOC", "\x1b[D", "\x1bOD") or not v.chave:
        return
    tipo, caminho = v.chave
    if k in ("\x1b[C", "\x1bOC"):
        if tipo == "d":
            ABERTAS.add(caminho)
    elif tipo == "d" and caminho in ABERTAS:
        ABERTAS.discard(caminho)
    else:
        parent = os.path.dirname(caminho)
        if parent != REPO and parent.startswith(REPO + "/"):
            v.chave = ("d", parent)
    v.recoletar.set()


if __name__ == "__main__":
    Viewer(coletar, intervalo=2.0, cursor=True, ao_enter=enter, ao_tecla=on_key, chave_inicial=("d", REPO), rodape=footer).rodar()
