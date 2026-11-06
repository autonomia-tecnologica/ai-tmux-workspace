#!/usr/bin/env python3
"""Painel de repos: grupos de settings.json, cada repo com a bolinha do estado do agente.

  ○ vazio    sessão sem texto           ● verde   agente (ou subagente) trabalhando
  ● amarelo  parado, mas rodando algo         ● vermelho parado com texto / pergunta

Ao lado do nome, dois ícones sempre presentes:
  branch  = git local: esmaecido sem git · normal limpo · amarelo com trabalho não commitado
  GitHub  = remoto:    esmaecido não está no remoto · normal em dia · amarelo ahead/behind do upstream
                       (fetch a cada 5 min)
"""
import json
import subprocess
import sys

import lib
from viewer import AZUL, B, CINZA, DIM, R, VERDE, AMARELO, VERMELHO, Linha, Viewer

BOLA = {"ocioso": f"{CINZA}○{R}", "trabalha": f"{VERDE}●{R}", "espera": f"{AMARELO}●{R}",
        "pergunta": f"{VERMELHO}●{R}"}
GH = "\uf09b"   # nerd font: logo do GitHub
BRANCH = "\ue725"   # nerd font: git branch
ESMAECIDO = "\x1b[38;5;238m"


def icone_git(repo):
    """Dois ícones, sempre presentes: branch (git local) e logo do GitHub (remoto)."""
    est, sujo = lib.estado_git(repo)
    if est is None:                                   # sem git: os dois esmaecidos
        return f"{ESMAECIDO}{BRANCH} {GH}{R}"
    git = f"{AMARELO}{BRANCH}{R}" if sujo else BRANCH
    if est == "local":
        remoto = f"{ESMAECIDO}{GH}{R}"
    else:
        remoto = f"{AMARELO}{GH}{R}" if est == "dessinc" else GH
    return f"{git} {remoto}"


COLAPSADOS = lib.ESTADO / "colapsados.json"
ATUAL = lib.repo_atual()


def colapsados():
    try:
        return set(json.loads(COLAPSADOS.read_text()))
    except Exception:
        return set()


def coletar(v):
    grupos, est, col = lib.ler_repos(), None, colapsados()
    principal = lib.ler_principal()
    est = lib.estados_por_repo(grupos + ([("", [principal])] if principal else []))
    cab = []   # the REPOS title lives in the panel's top bar (panel_tabs.py repos)
    corpo = []
    if principal:   # main repo: outside the groups, always on top, bold in the default foreground (blue when it is this window's repo)
        rnome, p = principal
        cor = AZUL if p == ATUAL else ""
        corpo.append(Linha(f" {BOLA[est.get(p, 'ocioso')]} {cor}{B}{rnome}{R} {icone_git(p)}", ("r", p)))
    for nome, repos in grupos:
        seta = "▸" if nome in col else "▾"
        # a bolinha do grupo fechado mostra o pior estado de dentro
        pior = max((est.get(p, "ocioso") for _, p in repos), key=lib.PRIO.get, default="ocioso")
        marca = f" {BOLA[pior]}" if nome in col and pior != "ocioso" else ""
        corpo.append(Linha(f" {CINZA}{seta} {nome.upper()}{R}{marca}", ("g", nome)))
        if nome in col:
            continue
        for rnome, p in repos:
            sel = f"{AZUL}{B}" if p == ATUAL else ""
            corpo.append(Linha(f"   {BOLA[est.get(p, 'ocioso')]} {sel}{rnome}{R} {icone_git(p)}", ("r", p)))
    return cab, corpo


def on_key(v, k):
    """Right expands the group under the cursor, Left collapses it (like the files tree)."""
    if k not in ("\x1b[C", "\x1bOC", "\x1b[D", "\x1bOD") or not v.chave or v.chave[0] != "g":
        return
    col = colapsados()
    right = k in ("\x1b[C", "\x1bOC")
    if (v.chave[1] in col) != right:
        return
    col ^= {v.chave[1]}
    lib.ESTADO.mkdir(parents=True, exist_ok=True)
    COLAPSADOS.write_text(json.dumps(sorted(col)))
    v.recoletar.set()


def enter(v, chave, tecla):
    tipo, valor = chave
    if tipo == "g":
        col = colapsados()
        col ^= {valor}
        lib.ESTADO.mkdir(parents=True, exist_ok=True)
        COLAPSADOS.write_text(json.dumps(sorted(col)))
        v.recoletar.set()
    else:
        subprocess.run([str(lib.BASE / "bin" / "workspace"), "open", valor], timeout=20,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def perde_foco(v):
    """Leaving the pane (or window) puts the cursor back on this window's own repo."""
    v.chave = ("r", ATUAL)
    v.recoletar.set()
    v._acordar()


if __name__ == "__main__":
    Viewer(coletar, intervalo=1.0, cursor=True, ao_enter=enter, ao_tecla=on_key, chave_inicial=("r", ATUAL),
           ao_perde_foco=perde_foco).rodar()
