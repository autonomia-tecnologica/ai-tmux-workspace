#!/usr/bin/env python3
"""Painel de repos: grupos de config.yaml, cada repo com a bolinha do estado do agente.

  ○ vazio    sessão sem texto           ● verde   agente (ou subagente) trabalhando
  ● amarelo  parado, mas rodando algo         ● vermelho parado com texto / pergunta

Ao lado do nome, dois ícones sempre presentes:
  branch  = git local: esmaecido sem git · normal limpo · amarelo com trabalho não commitado
  GitHub  = remoto:    esmaecido não está no remoto · normal em dia · amarelo ahead/behind do upstream
                       (fetch a cada 5 min)
"""
import subprocess
import sys

import glyphs
import lib
from tree_state import Expander
from viewer import AZUL, B, CINZA, DIM, R, VERDE, AMARELO, VERMELHO, Linha, Viewer



def icone_git(repo):
    """Dois ícones, sempre presentes: branch (git local) e logo do GitHub (remoto)."""
    est, sujo = lib.estado_git(repo)
    return glyphs.git_icons(est, sujo)


GRUPOS = Expander(lib.ESTADO / "colapsados.json", padrao_aberto=True)   # groups start open; the file keeps the closed ones
ATUAL = lib.repo_atual()


def coletar(v):
    grupos, est = lib.ler_repos(), None
    principal = lib.ler_principal()
    est = lib.estados_por_repo(grupos + ([("", [principal])] if principal else []))
    cab = []   # the REPOS title lives in the panel's top bar (panel_tabs.py repos)
    corpo = []
    if principal:   # main repo: outside the groups, always on top, bold in the default foreground (blue when it is this window's repo)
        rnome, p = principal
        cor = AZUL if p == ATUAL else ""
        corpo.append(Linha(f" {glyphs.dot(est.get(p, 'ocioso'))} {cor}{B}{rnome}{R} {icone_git(p)}", ("r", p)))
    for nome, repos in grupos:
        aberto = GRUPOS.is_open(nome)
        seta = "▾" if aberto else "▸"
        # a bolinha do grupo fechado mostra o pior estado de dentro
        pior = max((est.get(p, "ocioso") for _, p in repos), key=lib.PRIO.get, default="ocioso")
        marca = f" {glyphs.dot(pior)}" if not aberto and pior != "ocioso" else ""
        corpo.append(Linha(f" {CINZA}{seta} {nome.upper()}{R}{marca}", ("g", nome)))
        if not aberto:
            continue
        for rnome, p in repos:
            sel = f"{AZUL}{B}" if p == ATUAL else ""
            corpo.append(Linha(f"   {glyphs.dot(est.get(p, 'ocioso'))} {sel}{rnome}{R} {icone_git(p)}", ("r", p)))
    return cab, corpo


def on_key(v, k):
    """Right expands the group under the cursor, Left collapses it (like the files tree)."""
    if k not in ("left", "right") or not v.chave or v.chave[0] != "g":
        return
    GRUPOS.handle_arrow(k, v.chave[1])
    v.recoletar.set()


def enter(v, chave, tecla):
    tipo, valor = chave
    if tipo == "g":
        GRUPOS.toggle(valor)
        v.recoletar.set()
    else:
        subprocess.run([lib.WS, "open", valor], timeout=20,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def perde_foco(v):
    """Leaving the pane (or window) puts the cursor back on this window's own repo."""
    v.chave = ("r", ATUAL)
    v.recoletar.set()
    v._acordar()


if __name__ == "__main__":
    Viewer(coletar, intervalo=1.0, cursor=True, ao_enter=enter, ao_tecla=on_key, chave_inicial=("r", ATUAL),
           ao_perde_foco=perde_foco).rodar()
