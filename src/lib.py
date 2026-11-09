"""Base comum dos painéis do workspace: lista de repos, estado dos agentes, repo da janela."""
import copy
import fcntl
import hashlib
import importlib.util
import json
import os
import re
import shlex
import subprocess
import time
from pathlib import Path

from i18n import t
import permission_state
import tmux
import yaml_min

BASE = Path(__file__).resolve().parent.parent   # raiz do repo
WS = os.path.join(os.environ.get("WS_DIR", str(BASE)), "bin", "workspace")   # the `workspace` script
HOME = Path.home()
ESTADO = Path(os.environ.get("WS_ESTADO", HOME / ".cache" / "workspace"))
SESSOES = ESTADO / "sessoes"
ESPERA_S = 8                 # ferramenta aberta há mais que isso = "esperando as coisas acontecerem"
ESPERA_TETO_S = 30 * 60      # sem nenhum evento há isso, a espera por ferramenta vira ocioso
SEM_EVENTO_S = 10 * 60       # trabalha sem ferramenta aberta e sem evento há isso = ocioso
OCIOSO_APOS_S = 6 * 3600     # sem evento há isso = ocioso (vermelho de Stop também envelhece)
ORFAO_IDADE_S = 24 * 3600    # arquivo de sessão morta há mais que isso é apagado
PRIO = {"ocioso": 0, "trabalha": 1, "espera": 2, "pergunta": 3}


def expandir(p):
    return os.path.realpath(os.path.expanduser(p))


class ErroConfig(ValueError):
    """The config file exists but cannot be read, so nothing may be written over it."""


class ErroSettings(ValueError):
    """The workspace-generated settings for a profile cannot be built safely (literal key, bad shape, I/O)."""


def config_path():
    """config.yaml único; mora fora do repo (o repo só guarda a estrutura). AT_WS_CONFIG troca o caminho."""
    return Path(os.environ.get("AT_WS_CONFIG", HOME / ".config" / "ai-tmux-workspace" / "config.yaml"))


def legado_path():
    """settings.json antigo (JSON), lido enquanto o config.yaml não existir. Com AT_WS_CONFIG, é o mesmo caminho em .json."""
    if "AT_WS_CONFIG" in os.environ:
        return config_path().with_suffix(".json")
    return HOME / ".config" / "ai-tmux-workspace" / "settings.json"


ERRO_CONFIG = None   # (caminho, linha ou None) da última leitura que falhou; None quando leu bem


def _ler_arquivo(path):
    """dict de um arquivo de config: JSON se o caminho terminar em .json, senão YAML."""
    texto = path.read_text(encoding="utf-8")
    dados = json.loads(texto) if path.suffix == ".json" else (yaml_min.loads(texto) or {})
    if not isinstance(dados, dict):
        raise ValueError("config root is not a mapping")
    return dados


def ler_config():
    """Config como dict: config_path() se existir; senão o settings.json antigo; senão {}. Erro de leitura dá {} e marca ERRO_CONFIG."""
    global ERRO_CONFIG
    ERRO_CONFIG = None
    try:
        if config_path().exists():
            return _ler_arquivo(config_path())
        if legado_path().exists():
            return _ler_arquivo(legado_path())
    except (OSError, ValueError) as e:
        ERRO_CONFIG = (str(config_path()), getattr(e, "linha", None))
    return {}


def precisa_migrar():
    """True quando só o settings.json antigo existe; a migração é oferecida, nunca feita sozinha."""
    return config_path().suffix != ".json" and not config_path().exists() and legado_path().exists()


def migrar_config():
    """Grava config_path() em YAML a partir do settings.json antigo, depois de reler o YAML e conferir que é igual.
    Não altera nem apaga o JSON. Devolve o caminho gravado; levanta OSError/ValueError se falhar."""
    dados = _ler_arquivo(legado_path())
    texto = yaml_min.dumps(dados)
    if yaml_min.loads(texto) != dados:
        raise ValueError("YAML does not read back to the same config")
    destino = config_path()
    if destino.exists():
        raise FileExistsError(str(destino))
    destino.parent.mkdir(parents=True, exist_ok=True)
    tmp = destino.with_name(destino.name + ".tmp")
    tmp.write_text(texto, encoding="utf-8")
    tmp.replace(destino)
    return destino


def agent_name():
    """Chosen agent (config.yaml "agent"); falls back to "claude" when unset or not in "profiles"."""
    profiles = ler_config().get("profiles", {})
    name = ler_config().get("agent", "claude")
    return name if name in profiles or name == "claude" else "claude"


def _profile_value(name):
    """Raw value of a profile in config.yaml: a command string (legacy) or a mapping."""
    profiles = ler_config().get("profiles", {})
    return profiles.get(name) if isinstance(profiles, dict) else None


def profile_command(name=None):
    """Command line of a profile. A mapping uses its `command` (default `claude`); a string is the command."""
    value = _profile_value(name or agent_name())
    if isinstance(value, dict):
        command = value.get("command")
        return command if isinstance(command, str) and command.strip() else "claude"
    return value if isinstance(value, str) and value.strip() else "claude"


def profile_settings(name=None):
    """Extra settings of a profile (the `settings` map of a mapping profile).

    None means a legacy string profile: its command owns `--settings`, so the workspace does not generate
    one. {} means a profile with no extra settings (the harness base is still generated)."""
    value = _profile_value(name or agent_name())
    if not isinstance(value, dict):
        return None
    settings = value.get("settings")
    if settings is None:
        return {}
    if not isinstance(settings, dict):
        raise ErroSettings(t("settings.err.profile", profile=str(name or agent_name())))
    return settings


def agent_command():
    """Command line of the chosen agent: config.yaml["profiles"][agent] (string or mapping), else `claude`."""
    return profile_command(agent_name())


def agent_identity():
    """Recorded identity of the chosen profile for `--continue` and permission-mode decisions.

    A mapping profile can use the same `claude` command as another profile, so the profile name is part
    of the identity; otherwise switching between two mapping profiles would look like the same
    environment and reapply the previous profile's mode/session. String profiles keep the old identity
    (their command line), which is what the recorded state already holds."""
    name = agent_name()
    command = agent_command()
    return f"{command} [{name}]" if isinstance(_profile_value(name), dict) else command


# --- workspace-generated settings -------------------------------------------------------------
# The workspace launches Claude Code with one generated settings file per profile: the harness hooks and
# deny rules from forcado.yaml (or the committed harness/settings.json until item 33 installs the
# harness), plus the profile's own `settings` map (URL, models, flags, apiKeyHelper). Access keys never
# enter this file: an env key with a literal value is refused and the credential must come from
# apiKeyHelper, which Claude Code runs at launch (F08 in the harness secrets rules).

HARNESS_PY = BASE / "harness" / "harness.py"
HARNESS_SETTINGS = BASE / "harness" / "settings.json"

_SETTINGS_FLAG = re.compile(r"(?:^|\s)--settings(?:=|\s|$)")
_SECRET_ENV = re.compile(
    r"(?i)(?:^|_)(?:API_KEY|AUTH_TOKEN|ACCESS_TOKEN|SESSION_TOKEN|REFRESH_TOKEN|TOKEN"
    r"|SECRET(?:_ACCESS)?_KEY|CLIENT_SECRET|SECRET|PASSWORD)(?:$|_)")
_KEY_LITERAL = re.compile(r"\b(?:sk-ant-[A-Za-z0-9_-]{10,}|sk-or-v1-[A-Za-z0-9]{20,}|sk-[A-Za-z0-9_-]{24,})")


def settings_dir():
    """Directory of the generated per-profile settings. Read at call time, like the other state under
    WS_ESTADO, so a caller that changes it (tests, a different state dir) gets its own files."""
    return Path(os.environ.get("WS_ESTADO", HOME / ".cache" / "workspace")) / "settings"


def _has_settings_flag(command):
    """True when the command already passes `--settings` (a legacy profile that owns its file)."""
    return bool(_SETTINGS_FLAG.search(command))


def _harness_module():
    """Load harness/harness.py without importing from src/ (the file is installed on its own)."""
    spec = importlib.util.spec_from_file_location("ws_harness_render", HARNESS_PY)
    if spec is None or spec.loader is None:
        raise ErroSettings(t("settings.err.base", path=str(HARNESS_PY), error="module not found"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _harness_hook_command(installed):
    """Script of the harness hook: the installed one when it exists, else this repo's copy. The installed
    harness is absent until item 33; pointing at a missing script would make Claude Code block every
    prompt (a non-zero hook aborts UserPromptSubmit), so the repo copy is the explicit fallback."""
    script = Path(installed) / "harness.py"
    return f'python3 "{script if script.is_file() else HARNESS_PY}"'


def _set_hook_command(settings, command):
    """Every harness hook uses the same command; rewrite it to the script that actually exists."""
    hooks = settings.get("hooks")
    if not isinstance(hooks, dict):
        return settings
    for groups in hooks.values():
        if not isinstance(groups, list):
            continue
        for group in groups:
            if not isinstance(group, dict):
                continue
            entries = group.get("hooks")
            for hook in entries if isinstance(entries, list) else []:
                if isinstance(hook, dict) and hook.get("type") == "command":
                    hook["command"] = command
    return settings


def harness_base_settings():
    """Hooks + deny of the forced harness. The installed harness is used when it has a forcado.yaml;
    until item 33 installs it, the committed harness/settings.json is the explicit fallback."""
    installed = Path(os.environ.get(
        "AT_HARNESS_DIR", HOME / ".local" / "share" / "ai-tmux-workspace" / "harness")).expanduser()
    command = _harness_hook_command(installed)
    if (installed / "forcado.yaml").is_file():
        try:
            module = _harness_module()
            return _set_hook_command(module.build_settings(module.load_rules(installed)), command)
        except ErroSettings:
            raise
        except Exception as error:
            raise ErroSettings(t("settings.err.render_harness", path=str(installed), error=str(error)))
    try:
        base = json.loads(HARNESS_SETTINGS.read_text(encoding="utf-8"))
        if not isinstance(base, dict):
            raise ValueError("settings root is not a mapping")
        return _set_hook_command(base, command)
    except (OSError, ValueError) as error:
        raise ErroSettings(t("settings.err.base", path=str(HARNESS_SETTINGS), error=str(error)))


def _merge_settings(base, profile):
    """Base first, profile on top. Harness hooks always win and `permissions.deny` is a union, so the
    forced part survives; every other profile key (env, model, apiKeyHelper, flags) is applied."""
    merged = copy.deepcopy(base)
    for key, value in profile.items():
        if key == "hooks":
            continue
        if key == "permissions" and isinstance(value, dict):
            permissions = merged.get("permissions")
            if not isinstance(permissions, dict):
                permissions = merged["permissions"] = {}
            for pkey, pvalue in value.items():
                if pkey == "deny":
                    if isinstance(pvalue, list):
                        deny = permissions.get("deny") if isinstance(permissions.get("deny"), list) else []
                        permissions["deny"] = deny + [item for item in pvalue if item not in deny]
                    continue
                if isinstance(pvalue, dict) and isinstance(permissions.get(pkey), dict):
                    permissions[pkey] = _merge_settings(permissions[pkey], pvalue)
                else:
                    permissions[pkey] = copy.deepcopy(pvalue)
        elif isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _merge_settings(merged[key], value)
        else:
            merged[key] = copy.deepcopy(value)
    return merged


def _check_secrets(value, path="settings"):
    if isinstance(value, dict):
        for key, item in value.items():
            if key == "env" and isinstance(item, dict):
                for name, val in item.items():
                    if isinstance(val, str) and val and _SECRET_ENV.search(str(name)):
                        raise ErroSettings(t("settings.err.secret", name=f"{path}.env.{name}"))
            _check_secrets(item, f"{path}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _check_secrets(item, f"{path}[{index}]")
    elif isinstance(value, str) and _KEY_LITERAL.search(value):
        raise ErroSettings(t("settings.err.secret_value", path=path))


def validate_settings(settings, profile=""):
    """Refuse a generated settings that carries a literal access key (F08)."""
    _check_secrets(settings, f"settings[{profile}]" if profile else "settings")


def _safe_profile_name(name):
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", str(name or "claude"))
    return "claude" if safe in ("", ".", "..") else safe


def ensure_profile_settings(name=None):
    """Write the profile's generated settings and return its path. None means a legacy string profile
    that already owns `--settings`. Raises ErroSettings when the settings cannot be generated safely."""
    name = name or agent_name()
    if _has_settings_flag(profile_command(name)):
        return None
    settings = _merge_settings(harness_base_settings(), profile_settings(name) or {})
    validate_settings(settings, name)
    directory = settings_dir()
    path = directory / f"{_safe_profile_name(name)}.json"
    try:
        directory.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f"{path.name}.{os.getpid()}.tmp")
        temporary.write_text(json.dumps(settings, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
                             encoding="utf-8")
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    except OSError as error:
        raise ErroSettings(t("settings.err.write", path=str(path), error=str(error)))
    return path


def agent_launch_command(mode=""):
    """Command line that actually launches the agent. Every profile runs with the workspace-generated
    settings (`--settings`); a legacy string profile that already passes `--settings` keeps its own file.
    Profiles other than `claude` run with tool search off: their Anthropic-compatible endpoints reject
    the `tool_definition` blocks Claude Code stores for tool search, which breaks `--continue` (decision
    D9 in the harness record). The recorded agent identity is `agent_identity()`; the env prefix, the
    generated settings and the permission mode are launch details, not a profile change. `mode` is
    omitted when the caller does not have a repo to read it from."""
    name = agent_name()
    cmd = agent_command()
    if _KEY_LITERAL.search(cmd):
        # a key in the command would travel in argv; F08 forbids it
        raise ErroSettings(t("settings.err.secret_value", path=f"profiles[{name}].command"))
    if not _has_settings_flag(cmd):
        settings_path = ensure_profile_settings(name)
        if settings_path is not None:
            cmd = f"{cmd} --settings {shlex.quote(str(settings_path))}"
    launch = cmd if name == "claude" else f"env ENABLE_TOOL_SEARCH=false {cmd}"
    return f"{launch} --permission-mode {shlex.quote(mode)}" if mode else launch


# Per-repo agent command of the last opening: {repo path: {"command": ...}}, plus a reserved "__notice__"
# key with the command whose migration prompt was already shown. A different command means the Claude Code
# environment changed (other profile/settings/API), so the old transcript must not be resumed.
AGENT_STATE = ESTADO / "agent.json"
NOTICE = "__notice__"


def _read_agent_state():
    try:
        data = json.loads(AGENT_STATE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_agent_state(state):
    try:
        AGENT_STATE.parent.mkdir(parents=True, exist_ok=True)
        tmp = AGENT_STATE.with_name(AGENT_STATE.name + ".tmp")
        tmp.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
        tmp.replace(AGENT_STATE)
    except OSError:
        pass


def project_dir(repo):
    """Claude Code project folder of a cwd: under ~/.claude/projects, every non-alphanumeric as `-`."""
    return HOME / ".claude" / "projects" / re.sub(r"[^A-Za-z0-9]", "-", repo)


def _newest_jsonl(folder):
    try:
        files = [f for f in folder.glob("*.jsonl") if f.is_file()]
        return max(files, key=lambda f: f.stat().st_mtime) if files else None
    except OSError:
        return None


def previous_transcript(repo):
    """Newest transcript of the repo: ~/.claude/projects/<project>/*.jsonl, else the `transcript_path`
    recorded in the session cache. Returns a Path or None."""
    repo = expandir(repo)
    found = _newest_jsonl(project_dir(repo))
    if found:
        return found
    best = None
    try:
        files = list(SESSOES.glob("*.json"))
    except OSError:
        return None
    for f in files:
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        cwd = d.get("cwd") if isinstance(d, dict) else None
        if not isinstance(cwd, str) or not cwd or expandir(cwd) != repo:
            continue
        path = d.get("transcript_path")
        if not isinstance(path, str) or not path.strip():
            continue
        path = Path(os.path.expanduser(path))
        if not path.is_file():
            continue
        ts = d.get("ts") if isinstance(d.get("ts"), (int, float)) else 0
        if best is None or ts >= best[0]:
            best = (ts, path)
    return best[1] if best else None


def opening_command(repo):
    """Command line that opens the repo's Claude pane. Same agent command as the last opening: today's
    `--continue` (new session when there is none). Different or not recorded yet: new session. When the
    environment changes, only the first repo opened with a previous transcript gets the i18n migration
    prompt (the "__notice__" key remembers that this command was already announced); the other repos start
    the new session silently. The command is recorded before the launch, so the prompt is never repeated
    for the same repo and change."""
    repo = expandir(repo)
    cmd = agent_identity()
    mode = permission_state.mode_for(repo, cmd, agent_name() == "claude")
    launch = agent_launch_command(mode)     # may raise ErroSettings: do not record a launch that did not happen
    permission_state.record_launch(repo, cmd, mode)
    state = _read_agent_state()
    previous = state.get(repo)
    prev_cmd = previous.get("command") if isinstance(previous, dict) else previous   # old format: the command
    state[repo] = {"command": cmd}
    if prev_cmd == cmd:
        _write_agent_state(state)
        return f"{launch} --continue || {launch}"
    transcript = previous_transcript(repo)
    prompt = ""
    if transcript and state.get(NOTICE) != cmd:
        state[NOTICE] = cmd
        prompt = t("agent.migrated", path=str(transcript))
    _write_agent_state(state)
    if prompt:
        return f"{launch} {shlex.quote(prompt)} || {launch}"
    return launch


def set_agent(name):
    """Persists the chosen agent in the config (other keys untouched). Refuses to overwrite a config that
    failed to read (ErroConfig). With only the old settings.json, the whole config is written as YAML."""
    cfg = ler_config()
    if ERRO_CONFIG:
        raise ErroConfig(str(config_path()))
    cfg["agent"] = name
    path = config_path()
    texto = json.dumps(cfg, indent=2, ensure_ascii=False) + "\n" if path.suffix == ".json" else yaml_min.dumps(cfg)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(texto, encoding="utf-8")
    tmp.replace(path)


def _entradas(caminhos):
    """Paths of a group list: each entry is a string, or {"path": ..., "siblings": [...]}. Bad entries are skipped."""
    res = []
    for c in caminhos if isinstance(caminhos, list) else []:
        if isinstance(c, dict):
            c = c.get("path")
        if isinstance(c, str) and c:
            res.append(expandir(c))
    return res


def ler_repos():
    """[(grupo, [(nome, caminho), ...]), ...] a partir de "groups" (categoria; cada entrada é caminho ou {path, siblings})."""
    grupos = []
    for nome_grupo, caminhos in ler_config().get("groups", {}).items():
        repos = [(os.path.basename(p) or p, p) for p in _entradas(caminhos)]
        grupos.append((nome_grupo, repos))
    return grupos


def ler_relacoes():
    """[(nome, [caminho, ...]), ...] de "relations": agrupamento por relação, independente de "groups". Não abre janelas."""
    relacoes = ler_config().get("relations", {})
    if not isinstance(relacoes, dict):
        return []
    return [(nome, _entradas(caminhos)) for nome, caminhos in relacoes.items()]


def _sibling_pairs(cfg):
    """{path: {siblings}}: "siblings" written on a "groups" entry holds in both directions."""
    pairs = {}
    for paths in cfg.get("groups", {}).values():
        for entry in paths if isinstance(paths, list) else []:
            if not isinstance(entry, dict) or not isinstance(entry.get("path"), str) or not entry["path"]:
                continue
            repo = expandir(entry["path"])
            declared = entry.get("siblings")
            for path in declared if isinstance(declared, list) else []:
                if isinstance(path, str) and path and expandir(path) != repo:
                    other = expandir(path)
                    pairs.setdefault(repo, set()).add(other)
                    pairs.setdefault(other, set()).add(repo)
    return pairs


def _inside(path, folder):
    return path == folder or path.startswith(folder + "/")


def siblings(path):
    """Siblings of a repo, without itself, sorted. Two sources:
    (a) "parent_folder": when the repo is inside it, every listed repo (main and groups) also inside it;
    (b) "siblings" written on a "groups" entry, which holds in both directions (even outside the parent folder)."""
    cfg = ler_config()
    target = expandir(path)
    found = set(_sibling_pairs(cfg).get(target, set()))
    folder = cfg.get("parent_folder")
    if isinstance(folder, str) and folder:
        folder = expandir(folder)
        if _inside(target, folder):
            listed = _entradas([cfg.get("main")]) + [p for _, rs in ler_repos() for _, p in rs]
            found |= {p for p in listed if p != target and _inside(p, folder)}
    return sorted(found)


def ler_principal():
    """(nome, caminho) do repo principal de config.yaml ("main"), ou None. Fica fora dos grupos."""
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
    # erro: true keeps the red state until the next event; other red states age out like the rest.
    if agora - ts > OCIOSO_APOS_S and not (est == "pergunta" and d.get("erro") is True):
        return "ocioso"
    if est == "trabalha":
        f = d.get("ferramenta")
        if f and f not in ("Task", "Agent"):
            if agora - ts > ESPERA_TETO_S:  # nenhum evento há 30 min: a ferramenta não vai voltar
                return "ocioso"
            if agora - d.get("ferr_ts", ts) > ESPERA_S:
                return "espera"
        if not f and agora - ts > SEM_EVENTO_S:     # sem evento há 10 min: sessão morreu sem avisar
            return "ocioso"
    return est


def cor_sessao(d, agora):
    """Displayed state of one session file. `agents` is informational only: the hook keeps the state
    at "trabalha" while background tasks run, so nothing here masks a red state."""
    return _cor(d, agora)


def repo_of(cwd, paths):
    """Longest path in `paths` that contains `cwd`, or None."""
    melhor = None
    for p in paths:
        if (cwd == p or cwd.startswith(p + "/")) and (melhor is None or len(p) > len(melhor)):
            melhor = p
    return melhor


def _pid_do(d):
    try:
        return max(0, int(d.get("claude_pid") or 0))
    except (TypeError, ValueError):
        return 0


def _pid_vivo(pid):
    """True se `pid` é um processo claude vivo; False se morreu, virou zumbi ou foi reaproveitado por
    outro programa; None se não dá para saber. Sem /proc (macOS) basta o kill(pid, 0)."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:     # existe, mas é de outro usuário
        pass
    except OSError:
        return None
    if not os.path.isdir("/proc/self"):
        return True
    try:
        with open(f"/proc/{pid}/stat", encoding="utf-8", errors="replace") as f:
            estado = f.read().rsplit(")", 1)[1].split()[0]
        if estado == "Z":
            return False
        with open(f"/proc/{pid}/comm", encoding="utf-8", errors="replace") as f:
            nome = f.read()
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            argv = f.read().replace(b"\0", b" ").decode("utf-8", "replace")
    except (OSError, IndexError):
        return False
    return "claude" in f"{nome} {argv}".lower()


def _outro_servidor(d, socket_atual):
    """True when the session was recorded on a tmux server other than the one in use."""
    sock = d.get("tmux_socket")
    return bool(sock and socket_atual and os.path.realpath(sock) != os.path.realpath(socket_atual))


def _vida(d, vivos, socket_atual):
    """True = viva, False = morta (pode apagar), None = não dá para saber.

    Com claude_pid > 0 o teste é o processo. Sem ele (arquivo antigo), o teste é o pane no servidor
    atual: pane que voltou ao shell já está fora de `vivos`.
    """
    pid = _pid_do(d)
    if pid > 0:
        return _pid_vivo(pid)
    if not d.get("pane"):
        return False
    if _outro_servidor(d, socket_atual) or vivos is None:
        return None
    return d["pane"] in vivos


def _sessoes_vivas(agora):
    """[(sid, state_json, color)] das sessões vivas no servidor tmux atual, mais nova primeiro.

    Lê só <sid>.json (pula *.status.json e *.lock). Apaga o arquivo de sessão morta há mais de
    ORFAO_IDADE_S; nunca apaga sessão viva nem a que não se consegue testar. Falha de unlink é ignorada.
    """
    try:
        arquivos = [f for f in SESSOES.glob("*.json") if "." not in f.stem]
    except OSError:
        return []
    vivos = tmux.live_panes()
    socket_atual = tmux.server_socket()
    res = []
    for f in arquivos:
        try:
            d = json.loads(f.read_text())
            if not isinstance(d, dict):
                d = {}
        except Exception:       # ilegível: sem pane nem pid, então órfão pela idade do arquivo
            d = {}
        try:
            ref = d["ts"] if isinstance(d.get("ts"), (int, float)) else f.stat().st_mtime
        except OSError:
            ref = agora
        vida = _vida(d, vivos, socket_atual)
        if vida is False and agora - ref > ORFAO_IDADE_S:
            try:
                f.unlink()
            except OSError:
                pass
            continue
        # Não mostrada: sem pane, morta, ou de outro servidor (pane id pode repetir entre servidores).
        if vida is False or not d.get("pane") or _outro_servidor(d, socket_atual):
            continue
        res.append((f.stem, d, cor_sessao(d, agora)))
    res.sort(key=lambda x: x[1].get("ts", 0), reverse=True)
    return res


def session_for_pane(pane_id, agora=None):
    """(sid, state_json, color) da sessão viva do pane no servidor atual (a mais nova, se houver
    mais de uma), ou None. Para a statusbar e quem precisa da sessão de um pane só."""
    if not pane_id:
        return None
    for entrada in _sessoes_vivas(agora or time.time()):
        if entrada[1].get("pane") == pane_id:
            return entrada
    return None


def live_sessions(paths, agora=None):
    """{repo_path: [(sid, state_json, color)]} of live Claude sessions inside `paths`, newest first.

    See _sessoes_vivas for what counts as live. Sessions without a cwd are skipped.
    """
    agora = agora or time.time()
    res = {}
    for sid, d, cor in _sessoes_vivas(agora):
        cwd = d.get("cwd")
        if not isinstance(cwd, str) or not cwd:
            continue
        repo = repo_of(expandir(cwd), paths)
        if repo:
            res.setdefault(repo, []).append((sid, d, cor))
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


def git_full(repo, *args, timeout=15, input=None):
    """(rc, stdout, stderr) of `git -C repo *args`; exceptions become rc 1 with the message in stderr."""
    env = dict(os.environ, GIT_OPTIONAL_LOCKS="0", LC_ALL="C.UTF-8")
    try:
        r = subprocess.run(["git", "-C", repo, *args], capture_output=True, text=True,
                           timeout=timeout, env=env, input=input)
        return r.returncode, r.stdout, r.stderr
    except Exception as e:
        # stdout keeps the old git() contract (the message); stderr makes it available to git_full().
        return 1, str(e), str(e)


def git(repo, *args, timeout=15, input=None):
    rc, out, _ = git_full(repo, *args, timeout=timeout, input=input)
    return rc, out


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
# Listed in config.yaml ("plugins": {perfil: [{"id", "path"}]}). Nothing is discovered: a folder
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
    """Raw entries of config.yaml["plugins"][perfil]: only what is listed there is ever loaded."""
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
    """(id, pasta, label traduzido, layout) of one config.yaml entry; raises ErroPlugin if invalid."""
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
    if comando == "perfil-cmd":        # bash: the command line of the chosen agent (mode per repo when given)
        repo = expandir(sys.argv[3]) if len(sys.argv) > 3 and sys.argv[3] else ""
        try:
            if repo:
                cmd = agent_identity()
                mode = permission_state.mode_for(repo, cmd, agent_name() == "claude")
                launch = agent_launch_command(mode)
                permission_state.record_launch(repo, cmd, mode)
                print(launch)
            else:
                print(agent_launch_command())
        except ErroSettings as error:
            print(error, file=sys.stderr)
            sys.exit(2)
    elif comando == "ensure-settings":   # bash reload: generate a profile's settings without launching
        try:
            path = ensure_profile_settings(perfil or agent_name())
        except ErroSettings as error:
            print(error, file=sys.stderr)
            sys.exit(2)
        if path:
            print(path)
    elif comando == "profile-names":     # bash reload: every profile whose generated settings must be refreshed
        profiles = ler_config().get("profiles", {})
        names = sorted(set(profiles) | {agent_name()}) if isinstance(profiles, dict) else [agent_name()]
        print("\n".join(names))
    elif comando == "opening-command" and len(sys.argv) > 2:   # bash: command line that opens a repo
        try:
            print(opening_command(sys.argv[2]))
        except ErroSettings as error:
            print(error, file=sys.stderr)
            sys.exit(2)
    elif comando == "repo-inicial":    # bash: path of the first repo to open on a fresh start (main, else first listed)
        print((ler_principal() or ler_repos()[0][1][0])[1])
    elif comando == "nomes-janelas":   # bash: names a window of profile PERFIL may have (for prune); empty = keep all
        from worktrees import all_window_names   # lazy: worktrees imports lib
        names = [ler_principal()[0]] if ler_principal() else []
        names += [n for g, r in ler_repos() for n, p in r]
        names += all_window_names()              # worktree windows: window_name is the path's basename
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
