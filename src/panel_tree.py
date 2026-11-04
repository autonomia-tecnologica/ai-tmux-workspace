#!/usr/bin/env python3
"""Árvore de arquivos do repo da janela. Enter abre/fecha pasta ou abre o arquivo no editor."""
import os
import shlex
import shutil
import subprocess

import lib
from viewer import AMARELO, AZUL, B, CINZA, DIM, R, VERDE, Linha, Viewer

REPO = lib.repo_atual()
IGNORA = {".git", "node_modules", "__pycache__", ".venv", "target", ".mypy_cache", ".pytest_cache"}
ABERTAS = {REPO}
LIMITE = 400


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


def coletar(v):
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
                corpo.append(Linha(f" {ind}{CINZA}{'▾' if aberta else '▸'}{R} {AZUL}{e.name}/{R}{mark}",
                                   ("d", e.path)))
                if aberta:
                    descer(e.path, nivel + 1)
            else:
                s = st.get(e.path)
                cor = "" if not s else (VERDE if s.strip() in ("??", "A") else AMARELO)
                corpo.append(Linha(f" {ind}  {cor}{e.name}{R}", ("f", e.path)))

    descer(REPO, 0)
    return cab, corpo


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
    else:
        subprocess.run(["tmux", "display-popup", "-E", "-w", "92%", "-h", "92%", "-d", REPO,
                        f"{editor()} {shlex.quote(caminho)}"],
                       timeout=None)


if __name__ == "__main__":
    Viewer(coletar, intervalo=2.0, cursor=True, ao_enter=enter, chave_inicial=("d", REPO)).rodar()
