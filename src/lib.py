"""Base comum dos painéis do workspace: lista de repos, estado dos agentes, repo da janela."""
import fcntl
import hashlib
import json
import os
import subprocess
import time
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent   # raiz do repo
HOME = Path.home()
ESTADO = Path(os.environ.get("WS_ESTADO", HOME / ".cache" / "workspace"))
SESSOES = ESTADO / "sessoes"
ESPERA_S = 8          # ferramenta aberta há mais que isso = "esperando as coisas acontecerem"
PRIO = {"ocioso": 0, "trabalha": 1, "espera": 2, "pergunta": 3}


def expandir(p):
    return os.path.realpath(os.path.expanduser(p))


def config_path():
    """settings.json único; mora fora do repo (o repo só guarda a estrutura)."""
    return Path(os.environ.get("AT_WS_CONFIG", HOME / ".config" / "ai-tmux-workspace" / "settings.json"))


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


_GH_CACHE = {}        # repo -> (ts, estado)
_FETCH_TS = {}        # repo -> ts do último fetch, só quando não dá para marcar em disco
GH_TTL = 10           # s entre releituras locais
FETCH_TTL = 300       # s entre fetches em segundo plano (só assim dá pra saber se está atrás)
_FETCH_DIR = ESTADO / "fetch"   # um marcador por repo: compartilhado entre as janelas (processos)


def _fetch_bg(repo):
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0", GIT_OPTIONAL_LOCKS="0")
    try:
        subprocess.run(["git", "-C", repo, "fetch", "--quiet"], timeout=30, env=env,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass


def _fetch_head(repo):
    """FETCH_HEAD do repo. Em worktree (ou submodulo) '.git' é arquivo com 'gitdir: <caminho>'."""
    g = os.path.join(repo, ".git")
    if os.path.isfile(g):
        try:
            with open(g, encoding="utf-8") as fh:
                linha = fh.read().strip()
        except OSError:
            return None
        if not linha.startswith("gitdir:"):
            return None
        d = linha[len("gitdir:"):].strip()
        g = d if os.path.isabs(d) else os.path.join(repo, d)
    return os.path.join(g, "FETCH_HEAD")


def _mtime(p):
    try:
        return os.path.getmtime(p) if p else 0
    except OSError:
        return 0


def _fetch_devido(repo, agora):
    """True se nenhuma janela buscou esse repo nos últimos FETCH_TTL s; None se não dá para marcar.

    Último fetch = o mais recente entre o FETCH_HEAD do git e um marcador em ESTADO/fetch (o marcador
    cobre fetch que falhou, que não reescreve FETCH_HEAD). O marcador fica travado enquanto checa e
    grava, então duas janelas no mesmo instante não disparam dois fetches.
    """
    marca = _FETCH_DIR / hashlib.sha1(repo.encode("utf-8")).hexdigest()
    try:
        _FETCH_DIR.mkdir(parents=True, exist_ok=True)
        with open(marca, "a+", encoding="utf-8") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            f.seek(0)
            try:
                marcado = float(f.read() or 0)
            except ValueError:
                marcado = 0
            ultimo = max(marcado, _mtime(_fetch_head(repo)))
            if agora - ultimo < FETCH_TTL:
                return False
            f.seek(0)
            f.truncate()
            f.write(repr(agora))
            f.flush()
            return True
    except OSError:
        return None


def estado_git(repo):
    """(estado, sujo): estado = None sem git · 'local' git criado mas não subiu · 'ok' subiu e em dia ·
    'dessinc' ahead/behind. sujo = há trabalho não commitado (modificado ou novo, fora do stash)."""
    agora = time.time()
    c = _GH_CACHE.get(repo)
    if c and agora - c[0] < GH_TTL:
        return c[1]
    if not os.path.exists(os.path.join(repo, ".git")):
        est = None
    else:
        due = _fetch_devido(repo, agora)
        if due is None:    # sem como marcar em disco: volta ao temporizador desta janela
            due = agora - _FETCH_TS.get(repo, 0) > FETCH_TTL
            if due:
                _FETCH_TS[repo] = agora
        if due:
            import threading
            threading.Thread(target=_fetch_bg, args=(repo,), daemon=True).start()
        rc, out = git(repo, "rev-list", "--left-right", "--count", "HEAD...@{u}", timeout=5)
        if rc != 0:
            # sem upstream: só é "local" se não houver remoto nenhum; com remoto (público ou
            # privado) o ícone do GitHub fica normal mesmo sem a branch rastreando
            rc2, remotes = git(repo, "remote", timeout=5)
            est = "ok" if rc2 == 0 and remotes.strip() else "local"
        else:
            ahead, behind = (int(x) for x in out.split())
            est = "dessinc" if ahead or behind else "ok"
    sujo = False
    if est is not None:
        rc, out = git(repo, "status", "--porcelain", timeout=5)
        sujo = rc == 0 and bool(out.strip())
    _GH_CACHE[repo] = (agora, (est, sujo))
    return est, sujo
