#!/usr/bin/env python3
"""Sessões salvas do Claude Code para o repo da janela (histórico, como no Orca):
título, última fala do agente, nº de mensagens, quando, modelo. Enter retoma a sessão."""
import json
import os
import re
import shlex
import subprocess
import time

import i18n
import lib
from viewer import AMARELO, AZUL, B, CINZA, DIM, R, VERDE, Linha, Viewer

REPO = lib.repo_atual()
PASTA = lib.HOME / ".claude" / "projects" / re.sub(r"[^A-Za-z0-9]", "-", REPO)
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


def ha(seg):
    for lim, key in ((86400, "ago_d"), (3600, "ago_h"), (60, "ago_min")):
        if seg >= lim:
            return i18n.t(f"sessions.{key}", n=int(seg // lim))
    return i18n.t("sessions.now")


def coletar(v):
    try:
        fs = sorted(PASTA.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
    except OSError:
        fs = []
    cab = [Linha(f" {B}{i18n.t('sessions.title')}{R} {CINZA}{i18n.t('sessions.header', n=len(fs))}{R}"), Linha("")]
    corpo = []
    agora = time.time()
    for f in fs[:60]:
        st = f.stat()
        chave = (f.name, st.st_size, st.st_mtime)
        if chave not in CACHE:
            try:
                CACHE[chave] = ler(f)
            except Exception:
                continue
        d = CACHE[chave]
        vivo = agora - st.st_mtime < 60
        corpo.append(Linha(f" {B}{d['titulo']}{R}", f.stem))
        corpo.append(Linha(f"  {CINZA}{i18n.t('sessions.agent')}{R} {DIM}{d['ultima']}{R}"))
        corpo.append(Linha(f"  {VERDE if vivo else CINZA}●{R} {CINZA}{i18n.t('sessions.msgs', n=d['n'])} · {ha(agora - st.st_mtime)}"
                           f" · {d['modelo']}{R}"))
        corpo.append(Linha(""))
    return cab, corpo


def enter(v, sid, tecla):
    subprocess.run(["tmux", "confirm-before", "-p", i18n.t("sessions.confirm"),
                    f"run-shell '{shlex.quote(str(lib.BASE / 'bin' / 'workspace'))} resume {sid} #{{window_id}}'"])


if __name__ == "__main__":
    Viewer(coletar, intervalo=10.0, cursor=True, ao_enter=enter).rodar()
