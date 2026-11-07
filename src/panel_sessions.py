#!/usr/bin/env python3
"""Sessões ativas (Claudes abertos) de todos os repos do workspace, agrupadas por repo: título, estado, última fala, nº de mensagens,
modelo. Subagentes em execução viram uma linha dentro da sessão pai. Enter foca o painel da sessão."""
import json
import os
import re
import subprocess
import time

import glyphs
import i18n
import lib
from viewer import AMARELO, B, CINZA, DIM, R, VERDE, VERMELHO, Linha, Viewer


CACHE = {}


def texto(msg):
    c = msg.get("content")
    if isinstance(c, str):
        return c
    return " ".join(b.get("text", "") for b in c or [] if isinstance(b, dict) and b.get("type") == "text")


def ler(f):
    dados = f.read_bytes()
    titulo = None
    for m in re.finditer(rb'"aiTitle"\s*:\s*"((?:[^"\\]|\\.)*)"', dados):
        titulo = json.loads(b'"' + m.group(1) + b'"')
    n = len(re.findall(rb'"type"\s*:\s*"(?:user|assistant)"', dados))
    ultima, modelo, primeira = "", "", ""
    for l in reversed(dados[-400_000:].splitlines()):
        if b'"assistant"' not in l:
            continue
        try:
            d = json.loads(l)
        except Exception:
            continue
        if d.get("type") != "assistant":
            continue
        modelo = modelo or d["message"].get("model", "")
        t = texto(d["message"]).strip()
        if t:
            ultima = t
            break
    if not titulo:
        for l in dados[:300_000].splitlines():
            if b'"user"' not in l:
                continue
            try:
                d = json.loads(l)
                t = texto(d["message"]).strip()
            except Exception:
                continue
            if d.get("type") == "user" and t and not t.startswith("<"):
                primeira = t
                break
    return {"titulo": titulo or primeira or i18n.t("sessions.untitled"), "ultima": " ".join(ultima.split()),
            "n": n, "modelo": modelo.removeprefix("claude-")}


def pasta(cwd):
    return lib.HOME / ".claude" / "projects" / re.sub(r"[^A-Za-z0-9]", "-", cwd)


def repo_de(cwd, caminhos):
    melhor = None
    for p in caminhos:
        if (cwd == p or cwd.startswith(p + "/")) and (melhor is None or len(p) > len(melhor)):
            melhor = p
    return melhor


def ativas(agora, caminhos):
    """{repo_path: [(sid, state_json, color)]} of live Claude panes inside configured repos, newest first."""
    vivos = lib._live_panes()
    res = {}
    for f in lib.SESSOES.glob("*.json"):
        try:
            d = json.loads(f.read_text())
            cwd = lib.expandir(d["cwd"])
        except Exception:
            continue
        if not d.get("pane") or (vivos is not None and d["pane"] not in vivos):
            continue
        repo = repo_de(cwd, caminhos)
        if repo:
            res.setdefault(repo, []).append((f.stem, d, lib.cor_sessao(d, agora)))
    for v in res.values():
        v.sort(key=lambda x: x[1].get("ts", 0), reverse=True)
    return res


def coletar(v):
    agora = time.time()
    principal = lib.ler_principal()
    repos = ([principal] if principal else []) + [r for _, rs in lib.ler_repos() for r in rs]
    por_repo = ativas(agora, [p for _, p in repos])
    total = sum(len(x) for x in por_repo.values())
    cab = [Linha(f" {CINZA}{i18n.t('sessions.header', n=total)}{R}"), Linha("")]
    corpo = []
    for rnome, rpath in repos:
        for sid, e, cor in por_repo.get(rpath, []):
            f = pasta(e["cwd"]) / f"{sid}.jsonl"
            try:
                st = f.stat()
                chave = (f.name, st.st_size, st.st_mtime)
                if chave not in CACHE:
                    CACHE[chave] = ler(f)
                d = CACHE[chave]
            except Exception:
                d = {"titulo": i18n.t("sessions.untitled"), "ultima": "", "n": 0, "modelo": ""}
            if rnome:
                corpo.append(Linha(f" {CINZA}{rnome.upper()}{R}"))
                rnome = None
            k = (e["pane"], rpath)
            corpo.append(Linha(f" {glyphs.dot(cor)} {B}{d['titulo']}{R}", k))
            corpo.append(Linha(f"   {CINZA}{i18n.t('sessions.agent')}{R} {DIM}{d['ultima']}{R}", cont=True))
            corpo.append(Linha(f"   {CINZA}{i18n.t('sessions.msgs', n=d['n'])} · {d['modelo']}{R}", cont=True))
            tipos = e.get("agent_types") or {}
            for a in e.get("agents") or []:
                corpo.append(Linha(f"   {VERDE}↳{R} {CINZA}{tipos.get(a, 'agent')}{R}", cont=True))
            corpo.append(Linha(""))
    return cab, corpo


def tm(*args):
    return subprocess.run(["tmux", *args], capture_output=True, text=True).stdout.strip()


def enter(v, chave, tecla):
    """Focus the session's pane: bring it forward in its repo window (it may be a hidden tab)."""
    pane, repo = chave
    sess = tm("display", "-p", "-t", pane, "#{session_name}")
    win = ""
    for linha in tm("list-windows", "-t", sess, "-F", "#{window_id}|#{@ws_repo}").splitlines():
        wid, _, r = linha.partition("|")
        if r == repo:
            win = wid
            break
    if win:
        subprocess.run([str(lib.BASE / "bin" / "workspace"), "tab-show", win, pane],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10)
        tm("select-window", "-t", win)
    else:
        tm("select-window", "-t", pane)
    tm("select-pane", "-t", pane)


if __name__ == "__main__":
    Viewer(coletar, intervalo=3.0, cursor=True, ao_enter=enter).rodar()
