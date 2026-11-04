#!/usr/bin/env python3
"""Resumo do GitHub do repo da janela (via gh): saúde do repo, PR da branch atual com checks,
PRs e issues abertos, CI recente e última release. Enter abre o item no navegador."""
import json
import subprocess
from datetime import datetime, timezone

import lib
from viewer import AMARELO, AZUL, B, CINZA, DIM, R, VERDE, VERMELHO, Linha, Viewer, abas

REPO = lib.repo_atual()


def gh(*args, tolerar=False):
    r = subprocess.run(["gh", *args], cwd=REPO, capture_output=True, text=True, timeout=30)
    if r.returncode != 0:
        if tolerar:
            return None
        msg = (r.stderr or r.stdout).strip().splitlines()
        raise RuntimeError(msg[0] if msg else "gh falhou")
    return json.loads(r.stdout or "null")


def ha(iso):
    try:
        s = (datetime.now(timezone.utc) - datetime.fromisoformat(iso.replace("Z", "+00:00"))).total_seconds()
    except Exception:
        return ""
    for lim, un in ((86400, "d"), (3600, "h"), (60, "min")):
        if s >= lim:
            return f"{int(s // lim)}{un}"
    return "agora"


def titulo(t):
    return Linha(""), Linha(f" {AZUL}{B}{t}{R}")


def coletar(v):
    cab = [abas("github"), Linha(f"{DIM} {'─' * 60}{R}")]
    try:
        info = gh("repo", "view", "--json",
                  "nameWithOwner,description,isPrivate,stargazerCount,forkCount,defaultBranchRef,pushedAt,url")
    except Exception as e:
        return cab, [Linha(f" {DIM}sem repositório GitHub aqui{R}"), Linha(f" {DIM}{e}{R}")]
    corpo = [Linha(f" {B}{info['nameWithOwner']}{R} {CINZA}{'privado' if info['isPrivate'] else 'público'}{R}"),
             Linha(f" {DIM}{info['description'] or 'sem descrição'}{R}"),
             Linha(f" {CINZA}★ {info['stargazerCount']} · forks {info['forkCount']} · "
                   f"base {(info['defaultBranchRef'] or {}).get('name', '?')} · push há {ha(info['pushedAt'])}{R}")]
    try:
        # PR da branch atual e o estado dos checks dele
        pr = gh("pr", "view", "--json", "number,title,state,reviewDecision,mergeable,statusCheckRollup,isDraft",
                tolerar=True)
        corpo += titulo("BRANCH ATUAL")
        if pr:
            cs = pr.get("statusCheckRollup") or []
            ok = sum(1 for c in cs if (c.get("conclusion") or c.get("state")) in ("SUCCESS", "NEUTRAL", "SKIPPED"))
            ruim = sum(1 for c in cs if (c.get("conclusion") or c.get("state")) in ("FAILURE", "ERROR", "TIMED_OUT", "CANCELLED"))
            pend = len(cs) - ok - ruim
            rev = {"APPROVED": f"{VERDE}aprovado{R}", "CHANGES_REQUESTED": f"{VERMELHO}mudanças pedidas{R}",
                   "REVIEW_REQUIRED": f"{AMARELO}revisão pendente{R}"}.get(pr.get("reviewDecision"), f"{CINZA}sem revisão{R}")
            conf = f" {VERMELHO}conflito{R}" if pr.get("mergeable") == "CONFLICTING" else ""
            corpo.append(Linha(f"  {VERDE}#{pr['number']}{R} {pr['title']}", ("pr", pr["number"])))
            corpo.append(Linha(f"  {rev}{conf}  checks: {VERDE}{ok}✓{R} {VERMELHO}{ruim}✗{R} {AMARELO}{pend}…{R}"))
        else:
            corpo.append(Linha(f"  {DIM}sem PR para esta branch{R}"))
        prs = gh("pr", "list", "--json", "number,title,isDraft,author,reviewDecision", "-L", "10")
        corpo += titulo(f"PULL REQUESTS {CINZA}({len(prs)}){R}")
        for p in prs:
            rasc = f"{CINZA}rascunho{R} " if p["isDraft"] else ""
            corpo.append(Linha(f"  {VERDE}#{p['number']}{R} {rasc}{p['title']} {CINZA}{p['author']['login']}{R}",
                               ("pr", p["number"])))
        if not prs:
            corpo.append(Linha(f"  {DIM}nenhum aberto{R}"))
        iss = gh("issue", "list", "--json", "number,title,assignees", "-L", "10")
        corpo += titulo(f"ISSUES {CINZA}({len(iss)}){R}")
        for i in iss:
            meu = f" {AZUL}@eu{R}" if any(a["login"] == "@me" for a in i["assignees"]) else ""
            corpo.append(Linha(f"  {AMARELO}#{i['number']}{R} {i['title']}{meu}", ("issue", i["number"])))
        if not iss:
            corpo.append(Linha(f"  {DIM}nenhuma aberta{R}"))
        runs = gh("run", "list", "--json", "databaseId,name,status,conclusion,headBranch,createdAt", "-L", "6")
        corpo += titulo("CI RECENTE")
        for r in runs:
            c = r["conclusion"] or r["status"]
            cor = VERDE if c == "success" else VERMELHO if c in ("failure", "cancelled", "timed_out") else AMARELO
            corpo.append(Linha(f"  {cor}●{R} {r['name']} {CINZA}{r['headBranch']} · {ha(r['createdAt'])}{R}",
                               ("run", r["databaseId"])))
        if not runs:
            corpo.append(Linha(f"  {DIM}sem execuções{R}"))
        rel = gh("release", "list", "--json", "tagName,publishedAt", "-L", "1", tolerar=True)
        corpo += titulo("ÚLTIMA RELEASE")
        corpo.append(Linha(f"  {rel[0]['tagName']} {CINZA}há {ha(rel[0]['publishedAt'])}{R}" if rel
                           else f"  {DIM}nenhuma{R}"))
    except Exception as e:
        corpo.append(Linha(f" {VERMELHO}{e}{R}"))
    return cab, corpo


def enter(v, chave, tecla):
    tipo, n = chave
    cmd = {"pr": ["pr", "view", str(n), "--web"], "issue": ["issue", "view", str(n), "--web"],
           "run": ["run", "view", str(n), "--web"]}[tipo]
    subprocess.Popen(["gh", *cmd], cwd=REPO, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


if __name__ == "__main__":
    Viewer(coletar, intervalo=60.0, cursor=True, ao_enter=enter).rodar()
