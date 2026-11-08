#!/usr/bin/env python3
"""Options screen (`ctrl+b o`): one popup with every option of the workspace in a single expanded tree.

Built-in folders: "Agent" (picks the command that runs in the Claude panes, config["agent"]; changing it
restarts the whole workspace), "Prompts" (config["prompts"], else the i18n defaults prompt.wheel.*; Enter or 1-9 types
the prompt into the window's active Claude tab, without submitting), "Ask Claude" (one-off question to
`claude -p --model haiku` in the window's repo) and "Conference" (two start modes: the window's
repo plus its config siblings, or a picker of the workspace repos; both create the conference and
open the conversation screen with a message box in the footer (Enter sends, Ctrl+X stops). Every
answer comes from an isolated headless Claude, never from the window's session; each participant
answers only about its own repo (silent when the message is not its subject), may read the sibling
repos for the cross-validation, reads the repo's harness and may change only its `BACKLOG.md`).
config["options"] appends more nodes:
  {"label": "...", "children": [...]}   folder (Enter, Space or the arrows collapse/expand)
  {"label": "...", "ask": "<model>"}    asks a question to `claude -p --model <model>` in the repo, shows the answer
  {"label": "...", "run": "cmd"}        runs a shell command in the repo, shows its output
  {"label": "...", "type": "text"}      types the text into the active Claude tab
Built-in "Microfone" leaf: records the default mic until you stop talking (mic.py) and types the transcript, no Enter.
Usage: options.py <window id>
"""
import contextlib
import io
import os
import shlex
import subprocess
import sys
import textwrap

import help
import i18n
import lib
import tmux
from tree_state import Expander
from viewer import B, DIM, ORANGE, R, Linha, Viewer, close_on_esc, popup_header, term_cols

MAX_W = 100
TIMEOUT_S = 300

PASTAS = Expander(padrao_aberto=True)   # everything starts expanded: all options on one screen
pending = None      # leaf chosen in the tree, run after the viewer closes


def prompt_list():
    custom = lib.ler_config().get("prompts")
    if isinstance(custom, list) and custom:
        return [p for p in custom if isinstance(p, str) and p.strip()]
    return [v for k, v in sorted(i18n._STRINGS.items()) if k.startswith("prompt.wheel.")]


def tree():
    lib.ler_config()     # refreshes lib.ERRO_CONFIG before it is read below
    nodes = []
    if lib.ERRO_CONFIG:
        caminho, linha = lib.ERRO_CONFIG
        texto = (i18n.t("config.err.read_line", path=caminho, line=linha) if linha
                 else i18n.t("config.err.read", path=caminho))
        nodes.append({"label": texto})
    elif lib.precisa_migrar():
        nodes.append({"label": i18n.t("options.migrate"), "migrate": True})
    nodes += [
        {"label": i18n.t("options.prompts"), "prompts": True,
         "children": [{"label": " ".join(p.split()), "type": p} for p in prompt_list()]},
        {"label": i18n.t("options.ask"), "children": [{"label": i18n.t("options.ask_haiku"), "ask": "haiku"}]},
        {"label": i18n.t("options.conference"), "children": [
            {"label": i18n.t("options.conference.siblings"), "conference": "siblings"},
            {"label": i18n.t("options.conference.selected"), "conference": "selected"},
        ]},
    ]
    agents = sorted(set(lib.ler_config().get("profiles", {})) | {"claude"})
    current = lib.agent_name()
    nodes.append({"label": i18n.t("options.agent"), "children": [
        {"label": ("● " if a == current else "○ ") + a, "agent": a} for a in agents]})
    nodes.append({"label": i18n.t("options.general"), "children": [{"label": i18n.t("options.mic"), "mic": True}]})
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
        if isinstance(node.get("children"), list) and PASTAS.is_open(key):
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
            mark = "▾" if PASTAS.is_open(key) else "▸"
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


def pai_de(key):
    """Parent key of a row ("0.1" -> "0"), None at the top level."""
    return key.rsplit(".", 1)[0] if "." in key else None


def on_enter(v, key, k):
    global pending
    node = node_of(key)
    if node is None:
        return
    if isinstance(node.get("children"), list):
        if k == "l":        # Right opens; Enter and Space toggle
            PASTAS.open(key)
        else:
            PASTAS.toggle(key)
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
        pasta = isinstance(node, dict) and isinstance(node.get("children"), list)
        novo = PASTAS.handle_arrow(k, v.chave, pai=pai_de, pasta=pasta)
        if novo != v.chave:         # leaf or collapsed folder: moved up, and the parent folder collapses
            PASTAS.close(novo)
            v.chave = novo
        v.recoletar.set()


def repo_and_command(window):
    repo = tmux.display("#{@ws_repo}", target=window) or os.getcwd()
    return repo, shlex.split(lib.agent_launch_command())


def confirm_security(name):
    """Before switching to a non-Claude profile, shows what security is lost and asks for an explicit
    confirmation. Non-Claude profiles lose the Claude auto classifier and run under bypassPermissions;
    the full list is one key away, on the same screen. Returns True to continue."""
    if name == "claude":
        return True
    title = i18n.t("options.agent.security.title", profile=name)
    show(title, i18n.t("options.agent.security.summary", profile=name))
    while True:
        try:
            answer = input(f" {ORANGE}{i18n.t('options.agent.security.confirm')}{R} ").strip().lower()
        except (KeyboardInterrupt, EOFError):
            return False
        if answer in ("s", "sim", "y", "yes"):
            return True
        if answer in ("d", "detalhes", "details"):
            show(title, i18n.t("options.agent.security.details", profile=name))
            continue
        return False


def switch_agent(name, window):
    """Saves the agent and restarts the whole workspace (Claude resumes with --continue)."""
    if name == lib.agent_name():
        return
    if not confirm_security(name):
        return
    try:
        lib.set_agent(name)
    except (OSError, ValueError):
        show(i18n.t("options.agent"), i18n.t("config.err.write"))
        return
    env = dict(os.environ, WS_SEM_ANEXAR="1")
    subprocess.Popen([lib.WS, "restart"], env=env, start_new_session=True,
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def conference_output(fn, *args, **kwargs):
    """Runs a conference action in-process and returns what it printed (stdout + stderr)."""
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
        fn(*args, **kwargs)
    return buf.getvalue().strip()


def workspace_repos(window_repo):
    """[(group, [(name, path), ...]), ...] for the picker. The window repo is always first (even
    when the config does not list it); repeated paths keep the first occurrence."""
    window_repo = lib.expandir(window_repo)
    entries = [("", [(os.path.basename(window_repo), window_repo)])]
    seen = {window_repo}

    def add(group, repos):
        keep = []
        for name, path in repos:
            path = lib.expandir(path)
            if path in seen or not os.path.isdir(path):
                continue
            seen.add(path)
            keep.append((name or os.path.basename(path), path))
        if keep:
            entries.append((group, keep))

    principal = lib.ler_principal()
    if principal:
        add("", [principal])
    for group, repos in lib.ler_repos():
        add(group, repos)
    return entries


def choose_repos(window_repo):
    """Multi-select screen of the workspace repos. Returns the marked paths in display order, or []
    when the screen is cancelled or nothing is marked. The window repo starts marked."""
    entries = workspace_repos(window_repo)
    paths = [path for _, repos in entries for _, path in repos]
    state = {"selected": {lib.expandir(window_repo)}, "done": False, "notice": ""}
    title, keys = i18n.t("options.conference.pick_title"), i18n.t("options.conference.pick_keys")

    def collect(v):
        cols = term_cols()
        body = [Linha()]
        if state["notice"]:
            body += [Linha(f" {DIM}{state['notice']}{R}"), Linha()]
        for group, repos in entries:
            if group:
                body.append(Linha(f" {DIM}{group}{R}"))
            for name, path in repos:
                mark = "●" if path in state["selected"] else "○"
                color = ORANGE if path == v.chave else ""
                body.append(Linha(f" {color}{mark} {name}{R}", chave=path))
        return popup_header(title, keys, cols), body

    def on_input(v, k):
        if k in ("\r", "\n"):
            if not state["selected"]:
                state["notice"] = i18n.t("options.conference.pick_none")
                v.recoletar.set()
                return True
            state["done"] = True
            v.parar = True
            v._acordar()
            return True
        if k == " ":
            if v.chave:
                state["selected"] = (state["selected"] - {v.chave}
                                     if v.chave in state["selected"]
                                     else state["selected"] | {v.chave})
                state["notice"] = ""
                v.recoletar.set()
            return True
        if k == "a":
            state["selected"] = set(paths)
            state["notice"] = ""
            v.recoletar.set()
            return True
        if k == "n":
            state["selected"].clear()
            state["notice"] = ""
            v.recoletar.set()
            return True
        return False

    def footer(_v):
        return f"{DIM}{i18n.t('options.conference.pick_footer', n=len(state['selected']))}{R}"

    Viewer(collect, intervalo=3600, cursor=True,
           chave_inicial=paths[0] if paths else None,
           ao_tecla=close_on_esc, entrada=on_input, rodape=footer).rodar()
    if not state["done"]:
        return []
    return [path for path in paths if path in state["selected"]]


def run_conference(node, window):
    """Conference leaves: start with the window repo plus its config siblings, or with the workspace
    repos chosen on the picker; both open the conversation screen right after the start."""
    import conference

    action = node["conference"]
    repo = tmux.display("#{@ws_repo}", target=window) or os.getcwd()
    if action == "selected":
        repos = choose_repos(repo)
        if not repos:
            return
        text_out = conference_output(conference.start, repos=repos, repo=repo)
    else:
        text_out = conference_output(conference.start, repo=repo)
    if conference.read_active():    # started: the point of starting is the message box
        conference.converse(repo=repo)
        return
    show(node["label"], text_out or i18n.t("options.no_output"))


def run_leaf(node, window):
    if "conference" in node:
        run_conference(node, window)
        return
    if "migrate" in node:
        try:
            destino = lib.migrar_config()
            texto = i18n.t("config.migrated", path=str(destino))
        except (OSError, ValueError):
            texto = i18n.t("config.err.migrate")
        show(node["label"], texto)
        return
    if "mic" in node:
        import mic
        mic.run(window)
        return
    if "agent" in node:
        switch_agent(node["agent"], window)
        return
    if "type" in node:
        subprocess.run([lib.WS, "type", window, node["type"]], check=False,
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
        Viewer(collect, intervalo=3600, cursor=True, ao_enter=on_enter, ao_tecla=on_key, wrap=True).rodar()
        if pending:
            run_leaf(pending, WINDOW)
