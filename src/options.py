#!/usr/bin/env python3
"""Options screen (`ctrl+b o`): one popup with every option of the workspace in a single expanded tree.

Built-in folders: "Prompts" (settings.json["prompts"], else the i18n defaults prompt.wheel.*; Enter or 1-9 types
the prompt into the window's active Claude tab, without submitting) and "Ask Claude" (one-off question to
`claude -p --model haiku` in the window's repo). settings.json["options"] appends more nodes:
  {"label": "...", "children": [...]}   folder (Enter, Space or the arrows collapse/expand)
  {"label": "...", "ask": "<model>"}    asks a question to `claude -p --model <model>` in the repo, shows the answer
  {"label": "...", "run": "cmd"}        runs a shell command in the repo, shows its output
  {"label": "...", "type": "text"}      types the text into the active Claude tab
Usage: options.py <window id>
"""
import os
import shlex
import subprocess
import sys
import textwrap

import help
import i18n
import lib
import tmux
from viewer import B, DIM, ORANGE, R, Linha, Viewer, close_on_esc, popup_header, term_cols

MAX_W = 100
TIMEOUT_S = 300

collapsed = set()   # everything starts expanded: all options on one screen
pending = None      # leaf chosen in the tree, run after the viewer closes


def prompt_list():
    custom = lib.ler_config().get("prompts")
    if isinstance(custom, list) and custom:
        return [p for p in custom if isinstance(p, str) and p.strip()]
    return [v for k, v in sorted(i18n._STRINGS.items()) if k.startswith("prompt.wheel.")]


def tree():
    nodes = [
        {"label": i18n.t("options.prompts"), "prompts": True,
         "children": [{"label": " ".join(p.split()), "type": p} for p in prompt_list()]},
        {"label": i18n.t("options.ask"), "children": [{"label": i18n.t("options.ask_haiku"), "ask": "haiku"}]},
    ]
    custom = lib.ler_config().get("options")
    if isinstance(custom, list):
        nodes += custom
    return nodes


def flatten(nodes, depth=0, path=()):
    """[(key, depth, node)] of the visible rows; key is the index path ("0.1")."""
    rows = []
    for i, node in enumerate(nodes):
        if not isinstance(node, dict) or not str(node.get("label", "")).strip():
            continue
        key = ".".join(map(str, path + (i,)))
        rows.append((key, depth, node))
        if isinstance(node.get("children"), list) and key not in collapsed:
            rows += flatten(node["children"], depth + 1, path + (i,))
    return rows


def collect(v):
    cols = term_cols()
    w = min(MAX_W, cols - 4)
    body = [Linha()]
    n_prompt = 0
    for key, depth, node in flatten(tree()):
        folder = isinstance(node.get("children"), list)
        pad = "  " * depth
        if folder:
            mark = "▸" if key in collapsed else "▾"
            body.append(Linha(f" {pad}{ORANGE}{B}{mark} {node['label']}{R}", chave=key))
            continue
        num = ""
        if "type" in node and key.split(".")[0] == "0":
            n_prompt += 1
            num = f"{n_prompt} " if n_prompt <= 9 else "  "
        lines = textwrap.wrap(node["label"], max(10, w - len(pad) - 5)) or [""]
        for i, ln in enumerate(lines):
            head = f"{ORANGE}{num}{R}" if i == 0 else "  " * (len(num) // 2)
            body.append(Linha(f" {pad}  {head}{ln}", chave=key if i == 0 else None, cont=i > 0))
    if len(body) == 1:
        body.append(Linha(f" {DIM}{i18n.t('options.empty')}{R}"))
    return popup_header(i18n.t('options.bar'), i18n.t('options.keys'), cols), body


def node_of(key):
    return next((n for k, _, n in flatten(tree()) if k == key), None)


def set_collapsed(key, value):
    (collapsed.add if value else collapsed.discard)(key)


def on_enter(v, key, k):
    global pending
    node = node_of(key)
    if node is None:
        return
    if isinstance(node.get("children"), list):
        set_collapsed(key, key not in collapsed if k != "l" else False)
        v.recoletar.set()
    else:
        pending = node
        v.parar = True
        v._acordar()


def on_key(v, k):
    if close_on_esc(v, k):
        return
    if len(k) == 1 and k in "123456789":
        typed = [n for _, _, n in flatten(tree()) if "type" in n and _.startswith("0.")]
        if int(k) <= len(typed):
            global pending
            pending = typed[int(k) - 1]
            v.parar = True
            v._acordar()
    elif k == "right" and v.chave is not None:
        on_enter(v, v.chave, "l")
    elif k == "left" and v.chave is not None:
        node = node_of(v.chave)
        if node and isinstance(node.get("children"), list) and v.chave not in collapsed:
            set_collapsed(v.chave, True)
        elif "." in v.chave:        # leaf or collapsed folder: move up to the parent and collapse it
            v.chave = v.chave.rsplit(".", 1)[0]
            set_collapsed(v.chave, True)
        v.recoletar.set()


def repo_and_command(window):
    repo = tmux.display("#{@ws_repo}", target=window) or os.getcwd()
    profile = lib.ler_config().get("profiles", {}).get(tmux.display("#{@ws_perfil}", target=window), "") or "claude"
    return repo, shlex.split(profile)


def run_leaf(node, window):
    if "type" in node:
        subprocess.run([str(lib.BASE / "bin" / "workspace"), "type", window, node["type"]], check=False,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return
    repo, base = repo_and_command(window)
    label = node["label"]
    try:
        if "ask" in node:
            question = input(f"\n {ORANGE}{label}{R}\n {i18n.t('options.ask.prompt')} ").strip()
            if not question:
                return
            print(f"\n {DIM}{i18n.t('options.running')}{R}")
            cmd = base + ["-p", "--model", str(node["ask"] or "haiku"), question]
            proc = subprocess.run(cmd, cwd=repo, capture_output=True, text=True, timeout=TIMEOUT_S, stdin=subprocess.DEVNULL)
        elif "run" in node:
            print(f"\n {DIM}{i18n.t('options.running')}{R}")
            proc = subprocess.run(node["run"], shell=True, cwd=repo, capture_output=True, text=True, timeout=TIMEOUT_S, stdin=subprocess.DEVNULL)
        else:
            return
        text = (proc.stdout + (("\n" + proc.stderr) if proc.returncode and proc.stderr else "")).strip()
        title = label
    except (KeyboardInterrupt, EOFError):
        return
    except subprocess.TimeoutExpired:
        text, title = i18n.t("options.timeout", s=TIMEOUT_S), label
    show(title, text or i18n.t("options.no_output"))


def show(title, text):
    def collect_result(v):
        cols = term_cols()
        body = [Linha()] + help.render(text, cols)
        return popup_header(title, i18n.t('options.result_keys'), cols), body

    Viewer(collect_result, intervalo=3600, ao_tecla=close_on_esc).rodar()


if __name__ == "__main__":
    WINDOW = sys.argv[1] if len(sys.argv) > 1 else ""
    if WINDOW:
        Viewer(collect, intervalo=3600, cursor=True, ao_enter=on_enter, ao_tecla=on_key).rodar()
        if pending:
            run_leaf(pending, WINDOW)
