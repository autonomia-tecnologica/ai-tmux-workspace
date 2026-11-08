#!/usr/bin/env python3
"""Sessões ativas (Claudes abertos) de todos os repos do workspace, agrupadas por repo: título, estado, última fala, nº de mensagens,
modelo, contexto e custo. Subagentes em execução viram uma linha dentro da sessão pai. Enter foca o painel da sessão.

Os transcripts são lidos de forma incremental (só o que foi acrescentado desde a última leitura); modelo, contexto e custo
vêm do <sid>.status.json gravado pelo statusline.py."""
import json
import os
import re
import subprocess
import time
from pathlib import Path

import glyphs
import i18n
import lib
import statusbar
import tmux
from viewer import AMARELO, B, CINZA, DIM, R, VERDE, VERMELHO, Linha, Viewer


PARTE = 1 << 20          # bytes lidos por vez: o transcript é consumido em pedaços, nunca inteiro
CABECA = 300_000         # a primeira fala do usuário (título de reserva) só é procurada no começo
CACHE = {}               # caminho do transcript -> estado da leitura (ver _empty_state)


def texto(msg):
    c = msg.get("content")
    if isinstance(c, str):
        return c
    return " ".join(b.get("text", "") for b in c or [] if isinstance(b, dict) and b.get("type") == "text")


def _empty_state():
    return {"ini": 0, "n": 0, "titulo": None, "primeira": None, "ultima": "", "modelo": ""}


def _process(bloco, ini, est):
    """Atualiza `est` com as linhas completas de `bloco`, que começa no byte `ini` do transcript."""
    for m in re.finditer(rb'"aiTitle"\s*:\s*"((?:[^"\\]|\\.)*)"', bloco):
        try:
            est["titulo"] = json.loads(b'"' + m.group(1) + b'"')
        except Exception:
            pass
    est["n"] += len(re.findall(rb'"type"\s*:\s*"(?:user|assistant)"', bloco))
    pos = ini
    for l in bloco.split(b"\n"):
        atual, pos = pos, pos + len(l) + 1
        if not (b'"assistant"' in l or (est["primeira"] is None and atual < CABECA and b'"user"' in l)):
            continue
        try:
            d = json.loads(l)
        except Exception:
            continue
        if not isinstance(d, dict):
            continue
        msg = d.get("message")
        if not isinstance(msg, dict):
            continue
        if d.get("type") == "assistant":
            est["modelo"] = msg.get("model") or est["modelo"]       # o último modelo não vazio vale
            t = texto(msg).strip()
            if t:
                est["ultima"] = t
        elif d.get("type") == "user" and est["primeira"] is None and atual < CABECA:
            t = texto(msg).strip()
            if t and not t.startswith("<"):
                est["primeira"] = t


def _advance(f, est):
    """Lê só o que foi acrescentado a `f` desde `est["ini"]`, em pedaços, sem passar de linhas completas."""
    if f.stat().st_size < est["ini"]:            # truncado ou trocado: recomeça
        est.update(_empty_state())
    with open(f, "rb") as fh:
        fh.seek(est["ini"])
        resto = b""
        while True:
            bloco = fh.read(PARTE)
            if not bloco:
                break
            dados = resto + bloco
            fim = dados.rfind(b"\n") + 1
            if fim:
                _process(dados[:fim], est["ini"], est)
                est["ini"] += fim
            resto = dados[fim:]


def ler(f):
    """Título, última fala, nº de mensagens e modelo do transcript `f`, pelo CACHE (incremental)."""
    est = CACHE.setdefault(str(f), _empty_state())
    _advance(f, est)
    return {"titulo": est["titulo"] or est["primeira"] or i18n.t("sessions.untitled"),
            "ultima": " ".join(est["ultima"].split()), "n": est["n"],
            "modelo": str(est["modelo"] or "").removeprefix("claude-")}


def pasta(cwd):
    return lib.HOME / ".claude" / "projects" / re.sub(r"[^A-Za-z0-9]", "-", cwd)


def transcript_of(entry, sid):
    """Transcript da sessão: `transcript_path` da entrada quando existe; senão a pasta do projeto pelo cwd."""
    tp = entry.get("transcript_path")
    if isinstance(tp, str) and tp.strip():
        return Path(os.path.expanduser(tp))
    return pasta(entry["cwd"]) / f"{sid}.jsonl"


def coletar(v):
    agora = time.time()
    principal = lib.ler_principal()
    repos = ([principal] if principal else []) + [r for _, rs in lib.ler_repos() for r in rs]
    por_repo = lib.live_sessions([p for _, p in repos], agora)
    total = sum(len(x) for x in por_repo.values())
    cab = [Linha(f" {CINZA}{i18n.t('sessions.header', n=total)}{R}"), Linha("")]
    corpo = []
    usados = set()
    for rnome, rpath in repos:
        for sid, e, cor in por_repo.get(rpath, []):
            caminho = transcript_of(e, sid)
            usados.add(str(caminho))
            try:
                d = ler(caminho)
            except Exception:
                d = {"titulo": i18n.t("sessions.untitled"), "ultima": "", "n": 0, "modelo": ""}
            st = statusbar.status_of(sid)
            ultima = " ".join((statusbar.clean_text(e.get("last_assistant_message")) or d["ultima"]).split())
            modelo = statusbar.clean_text(st.get("model")) or d["modelo"]
            if rnome:
                corpo.append(Linha(f" {CINZA}{rnome.upper()}{R}"))
                rnome = None
            k = (e["pane"], rpath)
            corpo.append(Linha(f" {glyphs.dot(cor)} {B}{d['titulo']}{R}", k))
            corpo.append(Linha(f"   {CINZA}{i18n.t('sessions.agent')}{R} {DIM}{ultima}{R}", cont=True))
            partes = [i18n.t("sessions.msgs", n=d["n"])] + ([modelo] if modelo else [])
            pct = statusbar.as_number(st.get("context_pct"))
            if pct is not None:
                partes.append(i18n.t("status.context", pct=f"{pct:.0f}"))
            custo = statusbar.as_number(st.get("cost_usd"))
            if custo is not None:
                partes.append(i18n.t("sessions.cost", v=f"{custo:.2f}".replace(".", ",")))
            corpo.append(Linha(f"   {CINZA}{' · '.join(partes)}{R}", cont=True))
            tipos = e.get("agent_types") or {}
            for a in e.get("agents") or []:
                corpo.append(Linha(f"   {VERDE}↳{R} {CINZA}{tipos.get(a, 'agent')}{R}", cont=True))
            corpo.append(Linha(""))
    for chave in [c for c in CACHE if c not in usados]:      # sessão que saiu da lista: libera a leitura
        del CACHE[chave]
    return cab, corpo


def enter(v, chave, tecla):
    """Focus the session's pane: bring it forward in its repo window (it may be a hidden tab)."""
    pane, repo = chave
    sess = tmux.run("display", "-p", "-t", pane, "#{session_name}")
    win = ""
    for linha in tmux.run("list-windows", "-t", sess, "-F", "#{window_id}|#{@ws_repo}").splitlines():
        wid, _, r = linha.partition("|")
        if r == repo:
            win = wid
            break
    if win:
        subprocess.run([lib.WS, "tab-show", win, pane],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10)
        tmux.run("select-window", "-t", win)
    else:
        tmux.run("select-window", "-t", pane)
    tmux.run("select-pane", "-t", pane)


if __name__ == "__main__":
    Viewer(coletar, intervalo=3.0, cursor=True, ao_enter=enter).rodar()
