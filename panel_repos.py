#!/usr/bin/env python3
"""Painel de repos: grupos de settings.json, cada repo com a bolinha do estado do agente.

  ○ vazio    agente parado          ● verde   agente (ou subagente) trabalhando
  ● amarelo  esperando algo rodar   ● vermelho esperando você responder
"""
import json
import subprocess
import sys

import lib
from viewer import AZUL, B, CINZA, DIM, R, VERDE, AMARELO, VERMELHO, Linha, Viewer

BOLA = {"ocioso": f"{CINZA}○{R}", "trabalha": f"{VERDE}●{R}", "espera": f"{AMARELO}●{R}",
        "pergunta": f"{VERMELHO}●{R}"}
COLAPSADOS = lib.ESTADO / "colapsados.json"
ATUAL = lib.repo_atual()


def colapsados():
    try:
        return set(json.loads(COLAPSADOS.read_text()))
    except Exception:
        return set()


def coletar(v):
    grupos, est, col = lib.ler_repos(), None, colapsados()
    est = lib.estados_por_repo(grupos)
    cab = [Linha(f" {B}REPOS{R}"), Linha(f"{DIM} {'─' * 60}{R}")]
    corpo = []
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
            corpo.append(Linha(f"   {BOLA[est.get(p, 'ocioso')]} {sel}{rnome}{R}", ("r", p)))
    return cab, corpo


def enter(v, chave, tecla):
    tipo, valor = chave
    if tipo == "g":
        col = colapsados()
        col ^= {valor}
        lib.ESTADO.mkdir(parents=True, exist_ok=True)
        COLAPSADOS.write_text(json.dumps(sorted(col)))
        v.recoletar.set()
    else:
        subprocess.run([str(lib.BASE / "workspace"), "open", valor], timeout=20,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


if __name__ == "__main__":
    Viewer(coletar, intervalo=1.0, cursor=True, ao_enter=enter, chave_inicial=("r", ATUAL)).rodar()
