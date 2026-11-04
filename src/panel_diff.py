#!/usr/bin/env python3
"""Mudanças atuais do repo da janela (como a aba de controle de código do Orca):
branch, total +/-, e a lista de arquivos alterados com +linhas/-linhas e status."""
import os
import shlex
import subprocess

import lib
from viewer import AMARELO, AZUL, B, CINZA, DIM, R, VERDE, VERMELHO, Linha, Viewer, abas

REPO = lib.repo_atual()


def coletar(v):
    rc, out = lib.git(REPO, "status", "--porcelain", "-uall", "-b")
    if rc != 0:
        return [abas("diff")], [Linha(f" {DIM}não é um repositório git{R}")]
    linhas = out.splitlines()
    ramo = linhas[0][3:].split("...")[0] if linhas else "?"
    st = {}
    for l in linhas[1:]:
        st[l[3:].split(" -> ")[-1].strip('"')] = l[:2]
    nums = {}
    _, ns = lib.git(REPO, "diff", "HEAD", "--numstat")
    for l in ns.splitlines():
        a, d, p = l.split("\t", 2)
        nums[p] = (a, d)
    ta = td = 0
    for a, d in nums.values():
        ta += int(a) if a.isdigit() else 0
        td += int(d) if d.isdigit() else 0
    cab = [abas("diff"), Linha(f"{DIM} {'─' * 60}{R}"),
           Linha(f" {AZUL}{B}{ramo}{R}   {VERDE}+{ta}{R} {VERMELHO}-{td}{R}"),
           Linha(f" {B}MUDANÇAS{R} {CINZA}{len(st)}{R}"), Linha("")]
    corpo = []
    for p in sorted(st, key=lambda p: (st[p] == "??", p)):
        s = st[p].strip()
        letra, cor = ("U", VERDE) if s in ("??", "A") else ("D", VERMELHO) if "D" in s else ("M", AMARELO)
        a, d = nums.get(p, (None, None))
        cont = ""
        if a is not None:
            cont = (f" {VERDE}+{a}{R}" if a not in ("0", "-") else "") + (f" {VERMELHO}-{d}{R}" if d not in ("0", "-") else "")
        pasta = os.path.dirname(p)
        corpo.append(Linha(f" {cor}{letra}{R} {os.path.basename(p)} {CINZA}{pasta}{R}{cont}", (letra, p)))
    if not st:
        corpo.append(Linha(f" {DIM}árvore limpa{R}"))
    return cab, corpo


def enter(v, chave, tecla):
    letra, p = chave
    if letra == "U":
        cmd = f"less -R {shlex.quote(os.path.join(REPO, p))}"
    else:
        cmd = f"git -C {shlex.quote(REPO)} -c color.ui=always diff HEAD -- {shlex.quote(p)} | less -RX"
    subprocess.run(["tmux", "display-popup", "-E", "-w", "92%", "-h", "92%", "-d", REPO, cmd])


if __name__ == "__main__":
    Viewer(coletar, intervalo=2.0, cursor=True, ao_enter=enter).rodar()
