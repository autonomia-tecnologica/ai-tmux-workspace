#!/usr/bin/env python3
"""Árvore de arquivos do repo da janela. Enter abre/fecha pasta ou abre o arquivo no editor."""
import os
import shlex
import shutil
import subprocess

import clipboard
import glyphs
import icons
import lib
from i18n import t
import opener
import tmux
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


def ignored_names(pasta, itens):
    """Names among `itens` that git ignores (one `git check-ignore` call per folder)."""
    if not itens:
        return set()
    env = dict(os.environ, GIT_OPTIONAL_LOCKS="0", LC_ALL="C.UTF-8")
    try:
        r = subprocess.run(["git", "-C", REPO, "check-ignore", "-z", "--stdin"], capture_output=True,
                           input="\0".join(e.path for e in itens) + "\0", text=True, timeout=5, env=env)
    except Exception:
        return set()
    if r.returncode not in (0, 1):
        return set()
    return {os.path.basename(x.rstrip("/")) for x in r.stdout.split("\0") if x}


def listar(pasta):
    try:
        itens = [e for e in os.scandir(pasta) if e.name not in IGNORA]
    except OSError:
        return []
    itens.sort(key=lambda e: (not e.is_dir(follow_symlinks=False), e.name.lower()))
    return itens[:LIMITE]


_last_cwd = [None]


def follow_terminal(v):
    """When the shell in the active terminal tab changes directory, expand that folder (only it,
    not its children) and move the selection there. Only reacts to a change, so manual navigation
    in the tree is not overridden."""
    pane = os.environ.get("TMUX_PANE")
    if not pane:
        return
    win = tmux.display("#{window_id}", target=pane)
    target = tmux.show_w(win, "@ws_tativo")
    if not target:
        target = tmux.run("list-panes", "-t", win, "-F", "#{pane_id}|#{@papel}")
        target = next((l.split("|")[0] for l in target.splitlines() if l.endswith("|terminal")), "")
    if not target:
        return
    cwd = os.path.realpath(tmux.display("#{pane_current_path}", target=target) or REPO)
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

    def descer(pasta, nivel, ignorada=False):
        itens = listar(pasta)
        ign = {e.name for e in itens} if ignorada else ignored_names(pasta, itens)
        for e in itens:
            ind = "  " * nivel
            if e.is_dir(follow_symlinks=False):
                aberta = e.path in ABERTAS
                mark = f" {glyphs.dirty_mark()}" if e.path in sujos else ""
                ic = glyphs.dir_icon(aberta, e.name in ign) if USE_ICONS else ""
                nome = f"{DIM}{e.name}/{R}" if e.name in ign else f"{AZUL}{e.name}/{R}"
                corpo.append(Linha(f" {ind}{CINZA}{glyphs.arrow(aberta)}{R} {ic}{nome}{mark}",
                                   ("d", e.path)))
                if aberta:
                    descer(e.path, nivel + 1, e.name in ign)
            else:
                s = st.get(e.path)
                cor = DIM if e.name in ign else "" if not s else (VERDE if s.strip() in ("??", "A") else AMARELO)
                ic = glyphs.file_icon(e.name, e.name in ign) if USE_ICONS else ""
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
        tmux.run("display-popup", "-E", "-w", "92%", "-h", "92%", "-d", REPO,
                 f"{editor()} {shlex.quote(caminho)}", timeout=None)


def _copied(path):
    try:
        return clipboard.copy_file(path)
    except Exception:
        return None


def copiar(v, chave):
    """Puts the file on the clipboard as a file, to paste elsewhere. Only files reach here (see acoes)."""
    if _copied(chave[1]):
        msg = t("tree.copied", name=os.path.basename(chave[1]))
    else:
        msg = t("tree.copy_failed")
    tmux.message(msg)


def acoes(v, chave):
    """Context menu (Espaço or right click). On a folder the action is shown but unavailable."""
    return [(t("tree.menu_copy"), chave[0] == "f", copiar)]


def on_key(v, k):
    """Right opens the folder under the cursor, Left closes it (or jumps to the parent folder)."""
    if k not in ("left", "right") or not v.chave:
        return
    tipo, caminho = v.chave
    if k == "right":
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
    Viewer(coletar, intervalo=2.0, cursor=True, ao_enter=enter, ao_tecla=on_key, chave_inicial=("d", REPO), rodape=footer,
           acoes=acoes).rodar()
