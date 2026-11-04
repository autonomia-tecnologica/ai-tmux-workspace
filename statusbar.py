#!/usr/bin/env python3
"""Barra de status do tmux:  repo selecionado • modelo atual • consumo do Claude.

Uso:  statusbar.py <repo> [perfil]      (o tmux chama isto a cada poucos segundos)
O consumo vem dos logs locais do Claude Code (~/.claude/projects), somado de forma incremental
por um processo em segundo plano — a barra só lê o cache, então nunca trava.
"""
import fcntl
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

import lib

PROJ = lib.HOME / ".claude" / "projects"
CACHE = lib.ESTADO / "uso.json"
BALDE = 600            # segundos por balde
JANELA_H, JANELA_S = 5, 7 * 86400


def tokens(u):
    return (u.get("input_tokens") or 0) + (u.get("output_tokens") or 0) + \
           (u.get("cache_creation_input_tokens") or 0)


def atualizar_uso():
    """Lê só o que cresceu desde a última vez em cada .jsonl e acumula tokens por balde."""
    lib.ESTADO.mkdir(parents=True, exist_ok=True)
    lock = open(lib.ESTADO / "uso.lock", "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return
    try:
        c = json.loads(CACHE.read_text())
    except Exception:
        c = {"arq": {}, "baldes": {}}
    agora = time.time()
    vistos = set()
    for f in PROJ.glob("*/*.jsonl"):
        try:
            st = f.stat()
        except OSError:
            continue
        if st.st_mtime < agora - JANELA_S:
            continue
        ini = c["arq"].get(str(f), 0)
        if ini > st.st_size:
            ini = 0
        if ini == st.st_size:
            continue
        with open(f, "rb") as fh:
            fh.seek(ini)
            bloco = fh.read()
        fim = bloco.rfind(b"\n") + 1
        for linha in bloco[:fim].splitlines():
            if b'"usage"' not in linha:
                continue
            try:
                d = json.loads(linha)
                m = d["message"]
                if not str(m.get("model", "")).startswith("claude-"):
                    continue
                chave = (m.get("id"), d.get("requestId"))
                if chave in vistos:
                    continue
                vistos.add(chave)
                ts = datetime.fromisoformat(d["timestamp"].replace("Z", "+00:00")).timestamp()
                k = str(int(ts // BALDE))
                c["baldes"][k] = c["baldes"].get(k, 0) + tokens(m["usage"])
            except Exception:
                continue
        c["arq"][str(f)] = ini + fim
    corte = int((agora - JANELA_S - 86400) // BALDE)
    c["baldes"] = {k: v for k, v in c["baldes"].items() if int(k) >= corte}
    c["ts"] = agora
    tmp = CACHE.with_suffix(".tmp")
    tmp.write_text(json.dumps(c))
    os.replace(tmp, CACHE)


def uso():
    try:
        c = json.loads(CACHE.read_text())
    except Exception:
        c = {"baldes": {}, "ts": 0}
    if time.time() - c.get("ts", 0) > 90:
        subprocess.Popen([sys.executable, __file__, "--atualizar-uso"], stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, start_new_session=True)
    agora = time.time()
    h5 = sum(v for k, v in c["baldes"].items() if int(k) * BALDE >= agora - JANELA_H * 3600)
    d7 = sum(v for k, v in c["baldes"].items() if int(k) * BALDE >= agora - JANELA_S)
    return h5, d7


def fmt(n):
    if n >= 1e6:
        return f"{n / 1e6:.1f}M".replace(".", ",")
    if n >= 1e3:
        return f"{n / 1e3:.0f}k"
    return str(n)


def modelo(repo):
    """Modelo da última resposta nesse repo (do log mais recente); senão o do perfil."""
    pasta = PROJ / re.sub(r"[^A-Za-z0-9]", "-", repo)
    try:
        logs = sorted(pasta.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
    except OSError:
        logs = []
    for f in logs[:2]:
        try:
            with open(f, "rb") as fh:
                fh.seek(0, 2)
                fh.seek(max(0, fh.tell() - 400_000))
                cauda = fh.read().decode("utf-8", "ignore")
        except OSError:
            continue
        achados = re.findall(r'"model":"([^"<][^"]*)"', cauda)
        if achados:
            return achados[-1].removeprefix("claude-")
    return "—"


def main():
    if "--atualizar-uso" in sys.argv:
        atualizar_uso()
        return
    repo = sys.argv[1] if len(sys.argv) > 1 else os.getcwd()
    perfil = sys.argv[2] if len(sys.argv) > 2 else ""
    h5, d7 = uso()
    nome = (os.path.basename(repo) or repo).replace("#", "##")
    sep = "#[fg=colour240] • "
    print(f"#[fg=colour75,bold] {nome}#[nobold]{sep}#[fg=colour250]{modelo(repo)}"
          f"{(' (' + perfil + ')') if perfil and perfil != 'cc' else ''}{sep}"
          f"#[fg=colour250]5h {fmt(h5)} tok · 7d {fmt(d7)}#[default]")


if __name__ == "__main__":
    main()
