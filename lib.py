"""Base comum dos painéis do workspace: lista de repos, estado dos agentes, repo da janela."""
import json
import os
import subprocess
import time
from pathlib import Path

BASE = Path(__file__).resolve().parent
HOME = Path.home()
ESTADO = Path(os.environ.get("WS_ESTADO", HOME / ".cache" / "workspace"))
SESSOES = ESTADO / "sessoes"
ESPERA_S = 8          # ferramenta aberta há mais que isso = "esperando as coisas acontecerem"
PRIO = {"ocioso": 0, "trabalha": 1, "espera": 2, "pergunta": 3}


def expandir(p):
    return os.path.realpath(os.path.expanduser(p))


def config_path():
    """settings.json único; mora fora do repo (o repo só guarda a estrutura)."""
    return Path(os.environ.get("AT_WS_CONFIG", HOME / ".config" / "at-ai-workspace" / "settings.json"))


def ler_config():
    try:
        return json.loads(config_path().read_text())
    except (OSError, ValueError):
        return {}


def ler_repos():
    """[(grupo, [(nome, caminho), ...]), ...] a partir de settings.json ("groups")."""
    grupos = []
    for nome_grupo, caminhos in ler_config().get("groups", {}).items():
        repos = []
        for c in caminhos:
            p = expandir(c)
            repos.append((os.path.basename(p) or p, p))
        grupos.append((nome_grupo, repos))
    return grupos


def repo_atual():
    """Repo da janela onde este painel roda (opção @ws_repo da janela do tmux)."""
    pane = os.environ.get("TMUX_PANE")
    if pane:
        try:
            r = subprocess.run(["tmux", "display", "-p", "-t", pane, "#{@ws_repo}"],
                               capture_output=True, text=True, timeout=3)
            if r.stdout.strip():
                return r.stdout.strip()
        except Exception:
            pass
    return os.getcwd()


def _cor(d, agora):
    est, ts = d.get("estado", "ocioso"), d.get("ts", 0)
    if agora - ts > 6 * 3600:
        return "ocioso"
    if est == "trabalha":
        f = d.get("ferramenta")
        if f and f not in ("Task", "Agent") and agora - d.get("ferr_ts", ts) > ESPERA_S:
            return "espera"
        if not f and agora - ts > 600:      # sem evento há 10 min: sessão morreu sem avisar
            return "ocioso"
    return est


def estados_por_repo(repos, agora=None):
    """{caminho_do_repo: 'ocioso'|'trabalha'|'espera'|'pergunta'} — vale a pior sessão do repo."""
    agora = agora or time.time()
    caminhos = [p for _, rs in repos for _, p in rs]
    res = {}
    try:
        arquivos = list(SESSOES.glob("*.json"))
    except OSError:
        return res
    for f in arquivos:
        try:
            d = json.loads(f.read_text())
            cwd = expandir(d["cwd"])
        except Exception:
            continue
        melhor = None
        for p in caminhos:
            if (cwd == p or cwd.startswith(p + "/")) and (melhor is None or len(p) > len(melhor)):
                melhor = p
        if melhor:
            c = _cor(d, agora)
            if PRIO[c] >= PRIO[res.get(melhor, "ocioso")]:
                res[melhor] = c
    return res


def git(repo, *args, timeout=15):
    env = dict(os.environ, GIT_OPTIONAL_LOCKS="0", LC_ALL="C.UTF-8")
    try:
        r = subprocess.run(["git", "-C", repo, *args], capture_output=True, text=True,
                           timeout=timeout, env=env)
        return r.returncode, r.stdout
    except Exception as e:
        return 1, str(e)
