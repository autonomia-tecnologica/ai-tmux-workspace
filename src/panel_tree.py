#!/usr/bin/env python3
"""Árvore de arquivos do repo da janela. Enter abre/fecha pasta ou abre o arquivo no editor."""
import os
import shlex
import subprocess

import clipboard
import glyphs
import icons
import lib
from i18n import t
import opener
import tmux
from tree_state import Expander
from viewer import AMARELO, AZUL, B, CINZA, DIM, R, VERDE, Linha, Viewer

REPO = lib.repo_atual()
IGNORA = {".git", "node_modules", "__pycache__", ".venv", "target", ".mypy_cache", ".pytest_cache"}
ABERTAS = Expander()   # folders expanded by the user (closed by default)
ABERTAS.open(REPO)
LIMITE = 400
USE_ICONS = icons.enabled()


def status_git():
    st = lib.git_status(REPO) or {}
    return {os.path.join(REPO, p): xy for p, xy in st.items()}


def ignored_names(pasta, itens):
    """Names among `itens` that git ignores (one `git check-ignore` call per folder)."""
    if not itens:
        return set()
    rc, out = lib.git(REPO, "check-ignore", "-z", "--stdin",
                      input="\0".join(e.path for e in itens) + "\0", timeout=5)
    if rc != 0:   # 1 = nenhum ignorado (saída vazia); erro cai aqui também
        return set()
    return {os.path.basename(x.rstrip("/")) for x in out.split("\0") if x}


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
        ABERTAS.open(os.path.join(REPO, *rel[:i + 1]))
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
                aberta = ABERTAS.is_open(e.path)
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


RENAME = {"ativo": False, "chave": None, "nome": ""}


def footer(v):
    """Full name of the selected entry (the row itself is cut at the pane width). While renaming,
    shows the prompt and the inline buffer with a block cursor."""
    if RENAME["ativo"]:
        return f"{B}{t('tree.rename_prompt')}{R} {RENAME['nome']}█"
    if not v.chave or v.chave[1] == REPO:
        return ""
    return os.path.relpath(v.chave[1], REPO) + ("/" if v.chave[0] == "d" else "")


def _popup_command(choice, caminho):
    """tmux command string that opens `caminho` in `choice` (fullscreen gives a 100%x100% popup)."""
    size = "100%" if choice.get("fullscreen") else "92%"
    line = f"{choice['command']} {shlex.quote(caminho)}"
    command = (f"display-popup -E -w {size} -h {size} -d {shlex.quote(REPO)} "
               f"{shlex.quote(line)}")
    return command.replace("#", "##")     # tmux expands formats in the command string


def abrir_com_editor(caminho):
    """Editor menu for the file type: the configured entries that exist, or the fallback chain."""
    escolhas = opener.choices(caminho)
    if not escolhas:
        tmux.message(t("editor.none"))
        return
    itens = [(e["name"], str(i + 1), _popup_command(e, caminho))
             for i, e in enumerate(escolhas)]
    tmux.menu(t("editor.menu_title", name=os.path.basename(caminho)), itens)


def enter(v, chave, tecla):
    tipo, caminho = chave
    if tipo == "d":
        ABERTAS.toggle(caminho)
        v.recoletar.set()
    elif opener.use_system(caminho) and opener.open_system(caminho):
        return
    else:
        abrir_com_editor(caminho)


def _state(v):
    return getattr(v, "recoletar", None)


def _renomear_erro(nome):
    """i18n message for an invalid new name, or "" when it is acceptable."""
    if not nome:
        return t("tree.rename_empty")
    if os.path.isabs(nome) or "/" in nome or "\\" in nome or nome in (".", ".."):
        return t("tree.rename_invalid")
    return ""


def renomear_para(v, chave, nome):
    """Renames the file in `chave` to `nome` (same folder). Returns True on success and moves the
    cursor to the new path; on any failure shows an i18n message and keeps the old path."""
    tipo, atual = chave
    if tipo != "f":
        return False
    erro = _renomear_erro(nome)
    if erro:
        tmux.message(erro)
        return False
    novo = os.path.join(os.path.dirname(atual), nome)
    if os.path.normpath(novo) == os.path.normpath(atual):   # Enter without editing: nothing to do
        return True
    if os.path.lexists(novo):
        tmux.message(t("tree.rename_exists", name=nome))
        return False
    rel_old, rel_new = os.path.relpath(atual, REPO), os.path.relpath(novo, REPO)
    if lib.git(REPO, "ls-files", "--error-unmatch", rel_old)[0] == 0:
        ok = lib.git(REPO, "mv", "--", rel_old, rel_new)[0] == 0
    else:
        try:
            os.rename(atual, novo)
            ok = True
        except OSError:
            ok = False
    if ok:
        v.chave = ("f", novo)
        evento = _state(v)
        if evento:
            evento.set()
        tmux.message(t("tree.renamed", name=nome))
    else:
        tmux.message(t("tree.rename_failed", name=nome))
    return ok


def renomear(v, chave):
    """Starts the inline text input with the current basename: the main Viewer's `entrada` callback
    consumes the keys until Enter (commit) or Esc (cancel)."""
    if chave[0] != "f":
        return
    RENAME.update(ativo=True, chave=chave, nome=os.path.basename(chave[1]))
    v.recoletar.set()


def on_input(v, k):
    """Inline rename editor. Returns True while it consumes the key, so navigation is blocked."""
    if not RENAME["ativo"]:
        return False
    if k == "esc":
        RENAME.update(ativo=False, chave=None, nome="")
        v.recoletar.set()
        return True
    if k.startswith("\x1b"):        # arrows and other escape sequences do not edit or cancel
        return True
    if k in ("\r", "\n"):
        chave, nome = RENAME["chave"], RENAME["nome"]
        RENAME.update(ativo=False, chave=None, nome="")
        renomear_para(v, chave, nome)
        v.recoletar.set()
        return True
    if k in ("\x7f", "\x08"):       # Backspace
        RENAME["nome"] = RENAME["nome"][:-1]
        v.recoletar.set()
        return True
    if k.isprintable():
        RENAME["nome"] += k
        v.recoletar.set()
    return True


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
    """Context menu (Espaço or right click). On a folder the actions are shown but unavailable."""
    arquivo = chave[0] == "f"
    return [(t("tree.menu_copy"), arquivo, copiar),
            (t("tree.menu_rename"), arquivo, renomear)]


def pai_de(caminho):
    """Parent folder inside the repo, or None (the repo root is not a row)."""
    parent = os.path.dirname(caminho)
    return parent if parent != REPO and parent.startswith(REPO + "/") else None


def on_key(v, k):
    """Right opens the folder under the cursor, Left closes it (or jumps to the parent folder)."""
    if k not in ("left", "right") or not v.chave:
        return
    tipo, caminho = v.chave
    novo = ABERTAS.handle_arrow(k, caminho, pai=pai_de, pasta=tipo == "d")
    if novo != caminho:
        v.chave = ("d", novo)
    v.recoletar.set()


if __name__ == "__main__":
    Viewer(coletar, intervalo=2.0, cursor=True, ao_enter=enter, ao_tecla=on_key, chave_inicial=("d", REPO), rodape=footer,
           acoes=acoes, entrada=on_input).rodar()
