"""Base comum dos painéis do workspace: lista de repos, estado dos agentes, repo da janela."""
import fcntl
import hashlib
import json
import os
import re
import subprocess
import time
from pathlib import Path

from i18n import t
import tmux

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


def ler_principal():
    """(nome, caminho) do repo principal de settings.json ("main"), ou None. Fica fora dos grupos."""
    c = ler_config().get("main")
    if not isinstance(c, str) or not c:
        return None
    p = expandir(c)
    return (os.path.basename(p) or p, p)


def repo_atual():
    """Repo da janela onde este painel roda (opção @ws_repo da janela do tmux)."""
    pane = os.environ.get("TMUX_PANE")
    if pane:
        try:
            repo = tmux.display("#{@ws_repo}", target=pane)
            if repo:
                return repo
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


def cor_sessao(d, agora):
    """Displayed state of one session file: _cor, with Stop-only red masked while subagents run."""
    c = _cor(d, agora)
    if c == "pergunta" and d.get("stopped") and d.get("agents"):
        c = "trabalha"
    return c


def repo_of(cwd, paths):
    """Longest path in `paths` that contains `cwd`, or None."""
    melhor = None
    for p in paths:
        if (cwd == p or cwd.startswith(p + "/")) and (melhor is None or len(p) > len(melhor)):
            melhor = p
    return melhor


def live_sessions(paths, agora=None):
    """{repo_path: [(sid, state_json, color)]} of live Claude sessions inside `paths`, newest first.

    A session file without a pane is dropped, and so is one whose pane is gone; when tmux cannot be
    queried, only the pane-less files are dropped.
    """
    agora = agora or time.time()
    try:
        arquivos = list(SESSOES.glob("*.json"))
    except OSError:
        return {}
    vivos = tmux.live_panes()
    res = {}
    for f in arquivos:
        try:
            d = json.loads(f.read_text())
            cwd = expandir(d["cwd"])
        except Exception:
            continue
        # Orphan: pane-less legacy file, or its pane is gone (session died without SessionEnd).
        if not d.get("pane") or (vivos is not None and d["pane"] not in vivos):
            continue
        repo = repo_of(cwd, paths)
        if repo:
            res.setdefault(repo, []).append((f.stem, d, cor_sessao(d, agora)))
    for v in res.values():
        v.sort(key=lambda x: x[1].get("ts", 0), reverse=True)
    return res


def estados_por_repo(repos, agora=None):
    """{caminho_do_repo: 'ocioso'|'trabalha'|'espera'|'pergunta'} — vale a pior sessão do repo."""
    caminhos = [p for _, rs in repos for _, p in rs]
    res = {}
    for repo, sessoes in live_sessions(caminhos, agora).items():
        for _, _, c in sessoes:
            if PRIO[c] >= PRIO[res.get(repo, "ocioso")]:
                res[repo] = c
    return res


def git(repo, *args, timeout=15, input=None):
    env = dict(os.environ, GIT_OPTIONAL_LOCKS="0", LC_ALL="C.UTF-8")
    try:
        r = subprocess.run(["git", "-C", repo, *args], capture_output=True, text=True,
                           timeout=timeout, env=env, input=input)
        return r.returncode, r.stdout
    except Exception as e:
        return 1, str(e)


def git_status(repo, untracked=True):
    """`git status` como {caminho: xy}, ou None se não for repo. Usa -z: caminho sem aspas,
    e renomeio/cópia traz o caminho antigo num campo extra, que é pulado."""
    rc, out = git(repo, "status", "--porcelain", "-z", "-uall" if untracked else "-uno")
    if rc != 0:
        return None
    res = {}
    campos = out.split("\0")
    i = 0
    while i < len(campos):
        campo = campos[i]
        i += 1
        if len(campo) < 4:
            continue
        xy = campo[:2]
        res[campo[3:]] = xy
        if xy[0] in "RC":
            i += 1
    return res


def git_branch(repo):
    """Ramo atual; em HEAD solto, o hash curto."""
    rc, out = git(repo, "symbolic-ref", "--short", "-q", "HEAD")
    if rc == 0 and out.strip():
        return out.strip()
    rc, out = git(repo, "rev-parse", "--short", "HEAD")
    return out.strip() if rc == 0 and out.strip() else "?"


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


# --- Plugins -----------------------------------------------------------------------------------
# Listed in settings.json ("plugins": {perfil: [{"id", "path"}]}). Nothing is discovered: a folder
# is loaded only when its entry is there, and only from the fixed path written in the entry.
# The folder holds plugin.json: id, label (key of the plugin's own i18n/pt-BR.json), layout (same
# line format as REPO_TREE in bin/workspace; the root pane is the window itself and has role claude),
# needs (list of strings, validated but not acted on yet).
PLUGIN_ID = re.compile(r"[a-z0-9][a-z0-9-]*")
PLUGIN_ROLE = re.compile(r"[a-z][a-z0-9-]*")
PLUGIN_SPLIT = re.compile(r"[vh][bf]*")
PLUGIN_SIZE = re.compile(r"(\d+%?|TERM|LEFT|RIGHT)?")


class ErroPlugin(Exception):
    """Message already translated (i18n), ready to show the person."""


def plugins_listados(perfil):
    """Raw entries of settings.json["plugins"][perfil]: only what is listed there is ever loaded."""
    return ler_config().get("plugins", {}).get(perfil, [])


def _layout_plugin(id_, texto):
    """Checks the layout lines and returns them in the REPO_TREE format (role|parent|split|size|cmd)."""
    linhas, papeis = [], {"claude"}
    for n, linha in enumerate(texto.splitlines(), 1):
        s = linha.strip()
        if not s or s.startswith("#"):
            continue

        def falha(chave):
            return ErroPlugin(t("plugin.err.layout", id=id_, line=n, detail=t(chave)))

        campos = s.split("|", 4)
        if len(campos) != 5:
            raise falha("plugin.err.layout_fields")
        papel, pai, divisao, tamanho, comando = campos
        if papel == "rtabs":
            raise falha("plugin.err.layout_reserved")
        if not PLUGIN_ROLE.fullmatch(papel) or papel in papeis:
            raise falha("plugin.err.layout_role")
        if pai not in papeis:
            raise falha("plugin.err.layout_parent")
        if not PLUGIN_SPLIT.fullmatch(divisao):
            raise falha("plugin.err.layout_split")
        if not PLUGIN_SIZE.fullmatch(tamanho):
            raise falha("plugin.err.layout_size")
        if comando != "shell":
            raise falha("plugin.err.layout_cmd")
        papeis.add(papel)
        linhas.append(f"{papel}|{pai}|{divisao}|{tamanho}|{comando}")
    if not linhas:
        raise ErroPlugin(t("plugin.err.field", id=id_, field="layout"))
    return "\n".join(linhas)


def carregar_plugin(entrada):
    """(id, pasta, label traduzido, layout) of one settings.json entry; raises ErroPlugin if invalid."""
    if not isinstance(entrada, dict) or not isinstance(entrada.get("id"), str) \
            or not isinstance(entrada.get("path"), str):
        raise ErroPlugin(t("plugin.err.entry", entry=json.dumps(entrada, ensure_ascii=False)))
    id_ = entrada["id"]
    if not PLUGIN_ID.fullmatch(id_):
        raise ErroPlugin(t("plugin.err.id", id=id_))
    pasta = expandir(entrada["path"])
    arq = Path(pasta) / "plugin.json"
    try:
        dados = json.loads(arq.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise ErroPlugin(t("plugin.err.not_found", id=id_, path=pasta))
    except (OSError, ValueError):
        raise ErroPlugin(t("plugin.err.json", id=id_, path=str(arq)))
    if not isinstance(dados, dict):
        raise ErroPlugin(t("plugin.err.json", id=id_, path=str(arq)))
    if dados.get("id") != id_:
        raise ErroPlugin(t("plugin.err.id_mismatch", id=id_, found=str(dados.get("id"))))
    label = dados.get("label")
    if not isinstance(label, str) or not label:
        raise ErroPlugin(t("plugin.err.field", id=id_, field="label"))
    layout = dados.get("layout")
    if not isinstance(layout, str):
        raise ErroPlugin(t("plugin.err.field", id=id_, field="layout"))
    necessita = dados.get("needs", [])
    if not isinstance(necessita, list) or not all(isinstance(x, str) for x in necessita):
        raise ErroPlugin(t("plugin.err.field", id=id_, field="needs"))
    try:
        textos = json.loads((Path(pasta) / "i18n" / "pt-BR.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        textos = {}
    if label not in textos:
        raise ErroPlugin(t("plugin.err.label", id=id_, chave=label))
    return id_, pasta, textos[label], _layout_plugin(id_, layout)


if __name__ == "__main__":
    import sys
    comando = sys.argv[1]
    perfil = sys.argv[2] if len(sys.argv) > 2 else ""
    if comando == "perfil-cmd":        # bash: the command line of profile PERFIL ("" if unknown)
        print(ler_config().get("profiles", {}).get(perfil, ""))
    elif comando == "repo-inicial":    # bash: path of the first repo to open on a fresh start (main, else first listed)
        print((ler_principal() or ler_repos()[0][1][0])[1])
    elif comando == "nomes-janelas":   # bash: names a window of profile PERFIL may have (for prune); empty = keep all
        names = [ler_principal()[0]] if ler_principal() else []
        names += [n for g, r in ler_repos() for n, p in r]
        names += ["plugin:" + e["id"] for e in plugins_listados(perfil)
                  if isinstance(e, dict) and isinstance(e.get("id"), str)]
        print("\n".join(names))
    elif comando == "plugins":           # bash reads: "@@id<TAB>pasta<TAB>rótulo", then the layout lines
        for entrada in plugins_listados(perfil):
            try:
                id_, pasta, rotulo, arvore = carregar_plugin(entrada)
            except ErroPlugin as e:
                print(e, file=sys.stderr)
                continue
            print(f"@@{id_}\t{pasta}\t{rotulo}")
            print(arvore)
    elif comando == "plugin-ids":      # listed ids, valid or not (a broken plugin keeps its window)
        for entrada in plugins_listados(perfil):
            if isinstance(entrada, dict) and isinstance(entrada.get("id"), str):
                print(entrada["id"])
