#!/usr/bin/env python3
"""Current changes of the window repo (code-control tab):
branch, total +/-, and the list of changed files with +lines/-lines and status."""
import os
import shlex

import i18n
import lib
import tmux
from viewer import YELLOW, BLUE, B, GRAY, DIM, R, GREEN, RED, Row, Viewer

REPO = lib.current_repo()


def _numstat(output):
    """`git diff --numstat -z` as {new path: (+, -)}. With -z the path is unquoted;
    a rename comes as `a\\td\\t` followed by `old` and `new` in their own fields, and the key
    is the new path, the same one `git status` uses."""
    nums = {}
    fields = output.split("\0")
    i = 0
    while i < len(fields):
        field = fields[i]
        i += 1
        if not field:
            continue
        a, d, p = field.split("\t", 2)
        if not p:
            p = fields[i + 1]
            i += 2
        nums[p] = (a, d)
    return nums


def collect(v):
    st = lib.git_status(REPO)
    if st is None:
        return [], [Row(f" {DIM}{i18n.t('diff.not_repo')}{R}")]
    branch = lib.git_branch(REPO)
    _, ns = lib.git(REPO, "diff", "HEAD", "--numstat", "-z")
    nums = _numstat(ns)
    ta = td = 0
    for a, d in nums.values():
        ta += int(a) if a.isdigit() else 0
        td += int(d) if d.isdigit() else 0
    header = [Row(f" {BLUE}{B}{branch}{R}   {GREEN}+{ta}{R} {RED}-{td}{R}"),
           Row(f" {B}{i18n.t('diff.changes')}{R} {GRAY}{len(st)}{R}"), Row("")]
    body = []
    for p in sorted(st, key=lambda p: (st[p] == "??", p)):
        s = st[p].strip()
        letter, color = ("U", GREEN) if s in ("??", "A") else ("D", RED) if "D" in s else ("M", YELLOW)
        a, d = nums.get(p, (None, None))
        counts = ""
        if a is not None:
            counts = (f" {GREEN}+{a}{R}" if a not in ("0", "-") else "") + (f" {RED}-{d}{R}" if d not in ("0", "-") else "")
        folder = os.path.dirname(p)
        body.append(Row(f" {color}{letter}{R} {os.path.basename(p)} {GRAY}{folder}{R}{counts}", (letter, p)))
    if not st:
        body.append(Row(f" {DIM}{i18n.t('diff.clean')}{R}"))
    return header, body


def enter(v, key, _pressed):
    letter, p = key
    if letter == "U":
        cmd = f"less -R {shlex.quote(os.path.join(REPO, p))}"
    else:
        cmd = f"git -C {shlex.quote(REPO)} -c color.ui=always diff HEAD -- {shlex.quote(p)} | less -RX"
    tmux.popup(cmd, REPO)


if __name__ == "__main__":
    Viewer(collect, interval=2.0, cursor=True, on_enter=enter).run()
