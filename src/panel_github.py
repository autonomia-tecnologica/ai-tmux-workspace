#!/usr/bin/env python3
"""GitHub summary of the window repo (via gh): repo health, current-branch PR with checks,
open PRs and issues, recent CI and last release. Enter opens the item in the browser."""
import json
import subprocess
from datetime import datetime, timezone

import glyphs
import i18n
import lib
from viewer import YELLOW, BLUE, B, GRAY, DIM, R, GREEN, RED, Row, Viewer

REPO = lib.current_repo()


def gh(*args, tolerate=False):
    r = subprocess.run(["gh", *args], cwd=REPO, capture_output=True, text=True, timeout=30)
    if r.returncode != 0:
        if tolerate:
            return None
        msg = (r.stderr or r.stdout).strip().splitlines()
        raise RuntimeError(msg[0] if msg else i18n.t("github.failed"))
    return json.loads(r.stdout or "null")


def ago(iso):
    try:
        s = (datetime.now(timezone.utc) - datetime.fromisoformat(iso.replace("Z", "+00:00"))).total_seconds()
    except Exception:
        return ""
    for lim, un in ((86400, "d"), (3600, "h"), (60, "min")):
        if s >= lim:
            return f"{int(s // lim)}{un}"
    return i18n.t("github.now")


def heading(t):
    return Row(""), Row(f" {BLUE}{B}{t}{R}")


def collect(v):
    header = []
    try:
        info = gh("repo", "view", "--json",
                  "nameWithOwner,description,isPrivate,stargazerCount,forkCount,defaultBranchRef,pushedAt,url")
    except Exception as e:
        return header, [Row(f" {DIM}{i18n.t('github.no_repo')}{R}"), Row(f" {DIM}{e}{R}")]
    body = [Row(f" {B}{info['nameWithOwner']}{R} {GRAY}{i18n.t('github.private' if info['isPrivate'] else 'github.public')}{R}"),
             Row(f" {DIM}{info['description'] or i18n.t('github.no_description')}{R}"),
             Row(f" {GRAY}" + i18n.t("github.repo_meta", stars=info["stargazerCount"], forks=info["forkCount"],
                                   base=(info["defaultBranchRef"] or {}).get("name", "?"),
                                   ago=ago(info["pushedAt"])) + R)]
    try:
        # current-branch PR and the state of its checks
        pr = gh("pr", "view", "--json", "number,title,state,reviewDecision,mergeable,statusCheckRollup,isDraft",
                tolerate=True)
        body += heading(i18n.t("github.current_branch"))
        if pr:
            cs = pr.get("statusCheckRollup") or []
            ok = sum(1 for c in cs if (c.get("conclusion") or c.get("state")) in ("SUCCESS", "NEUTRAL", "SKIPPED"))
            bad = sum(1 for c in cs if (c.get("conclusion") or c.get("state")) in ("FAILURE", "ERROR", "TIMED_OUT", "CANCELLED"))
            pending = len(cs) - ok - bad
            rev = {"APPROVED": f"{GREEN}{i18n.t('github.approved')}{R}", "CHANGES_REQUESTED": f"{RED}{i18n.t('github.changes_requested')}{R}",
                   "REVIEW_REQUIRED": f"{YELLOW}{i18n.t('github.review_required')}{R}"}.get(pr.get("reviewDecision"), f"{GRAY}{i18n.t('github.no_review')}{R}")
            conf = f" {RED}{i18n.t('github.conflict')}{R}" if pr.get("mergeable") == "CONFLICTING" else ""
            body.append(Row(f"  {GREEN}#{pr['number']}{R} {pr['title']}", ("pr", pr["number"])))
            body.append(Row(f"  {rev}{conf}  {i18n.t('github.checks')} {GREEN}{ok}✓{R} {RED}{bad}✗{R} {YELLOW}{pending}…{R}"))
        else:
            body.append(Row(f"  {DIM}{i18n.t('github.no_pr')}{R}"))
        prs = gh("pr", "list", "--json", "number,title,isDraft,author,reviewDecision", "-L", "10")
        body += heading(f"{i18n.t('github.pull_requests')} {GRAY}({len(prs)}){R}")
        for p in prs:
            draft = f"{GRAY}{i18n.t('github.draft')}{R} " if p["isDraft"] else ""
            body.append(Row(f"  {GREEN}#{p['number']}{R} {draft}{p['title']} {GRAY}{p['author']['login']}{R}",
                               ("pr", p["number"])))
        if not prs:
            body.append(Row(f"  {DIM}{i18n.t('github.none_open_m')}{R}"))
        iss = gh("issue", "list", "--json", "number,title,assignees", "-L", "10")
        body += heading(f"{i18n.t('github.issues')} {GRAY}({len(iss)}){R}")
        for i in iss:
            mine = f" {BLUE}{i18n.t('github.me')}{R}" if any(a["login"] == "@me" for a in i["assignees"]) else ""
            body.append(Row(f"  {YELLOW}#{i['number']}{R} {i['title']}{mine}", ("issue", i["number"])))
        if not iss:
            body.append(Row(f"  {DIM}{i18n.t('github.none_open_f')}{R}"))
        runs = gh("run", "list", "--json", "databaseId,name,status,conclusion,headBranch,createdAt", "-L", "6")
        body += heading(i18n.t("github.recent_ci"))
        for r in runs:
            c = r["conclusion"] or r["status"]
            color = GREEN if c == "success" else RED if c in ("failure", "cancelled", "timed_out") else YELLOW
            body.append(Row(f"  {glyphs.ci_dot(color)} {r['name']} {GRAY}{r['headBranch']} · {ago(r['createdAt'])}{R}",
                               ("run", r["databaseId"])))
        if not runs:
            body.append(Row(f"  {DIM}{i18n.t('github.no_runs')}{R}"))
        rel = gh("release", "list", "--json", "tagName,publishedAt", "-L", "1", tolerate=True)
        body += heading(i18n.t("github.last_release"))
        body.append(Row(f"  {rel[0]['tagName']} {GRAY}{i18n.t('github.release_ago', ago=ago(rel[0]['publishedAt']))}{R}" if rel
                           else f"  {DIM}{i18n.t('github.none')}{R}"))
    except Exception as e:
        body.append(Row(f" {RED}{e}{R}"))
    return header, body


def enter(v, key, _pressed):
    kind, n = key
    cmd = {"pr": ["pr", "view", str(n), "--web"], "issue": ["issue", "view", str(n), "--web"],
           "run": ["run", "view", str(n), "--web"]}[kind]
    subprocess.Popen(["gh", *cmd], cwd=REPO, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


if __name__ == "__main__":
    Viewer(collect, interval=60.0, cursor=True, on_enter=enter).run()
