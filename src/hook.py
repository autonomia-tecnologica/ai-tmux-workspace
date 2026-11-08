#!/usr/bin/env python3
"""Hook do Claude Code: grava o estado da sessão para a bolinha do painel de repos.
Lê o JSON do hook no stdin, escreve ~/.cache/workspace/sessoes/<sessao>.json. Nunca falha."""
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import yaml_min  # noqa: E402  (stdlib-only, no lib import)

SESSOES = Path(os.environ.get("WS_ESTADO", Path.home() / ".cache" / "workspace")) / "sessoes"

# Deliberately does not import `lib`: this hook runs on every Claude Code event, so it must stay
# light and never fail on import. That is why the settings read in _notifications_enabled
# duplicates lib's config loader instead of calling it. Keep it that way.


def _one_line(text, n=100):
    text = " ".join(str(text or "").split())
    return text if len(text) <= n else text[: n - 1].rstrip() + "…"


# Reads the config itself rather than through lib; see the note near the top of this file.
# Same path rules as lib.ler_config: config.yaml, or the old settings.json while it does not exist.
def _notifications_enabled():
    try:
        if "AT_WS_CONFIG" in os.environ:
            cfg = Path(os.environ["AT_WS_CONFIG"])
            legado = cfg.with_suffix(".json")
        else:
            pasta = Path.home() / ".config" / "ai-tmux-workspace"
            cfg, legado = pasta / "config.yaml", pasta / "settings.json"
        arq = cfg if cfg.exists() else legado
        if arq.suffix == ".json":
            dados = json.loads(arq.read_text(encoding="utf-8"))
        else:
            dados = yaml_min.loads(arq.read_text(encoding="utf-8")) or {}
        return dados.get("notifications", True) is not False
    except Exception:
        return True


def _last_assistant_text(d):
    """End of the last assistant message: payload field, else the tail of the transcript."""
    if d.get("last_assistant_message"):
        return str(d["last_assistant_message"])
    try:
        with open(d["transcript_path"], "rb") as f:
            f.seek(0, os.SEEK_END)
            f.seek(max(0, f.tell() - 262144))
            lines = f.read().decode("utf-8", "replace").splitlines()
        for line in reversed(lines):
            try:
                m = json.loads(line)
            except ValueError:
                continue
            if m.get("type") != "assistant":
                continue
            content = (m.get("message") or {}).get("content")
            if isinstance(content, str):
                return content
            texts = [c.get("text", "") for c in content or [] if isinstance(c, dict) and c.get("type") == "text"]
            if any(t.strip() for t in texts):
                return "\n".join(texts)
    except Exception:
        pass
    return ""


def _snippet(d, ev):
    try:
        if ev == "Notification":
            return _one_line(d.get("message"))
        if ev == "Stop":
            return _one_line(_last_assistant_text(d)[-300:])
        ti = d.get("tool_input") or {}
        detail = ti.get("command") or ti.get("file_path") or ti.get("question") or ""
        return _one_line(f"{d.get('tool_name') or ''} {detail}".strip())
    except Exception:
        return ""


def _notify(d, ev):
    if not _notifications_enabled():
        return
    from notify import notify
    notify(os.path.basename((d.get("cwd") or "").rstrip("/\\")) or "Claude Code", _snippet(d, ev))


def main():
    d = json.load(sys.stdin)
    ev, sid = d.get("hook_event_name", ""), d.get("session_id")
    if not sid:
        return
    arq = SESSOES / f"{sid}.json"
    if ev == "SessionEnd":
        arq.unlink(missing_ok=True)
        return
    agora = time.time()
    try:
        e = json.loads(arq.read_text())
    except Exception:
        e = {}
    pane = os.environ.get("TMUX_PANE")
    if ev == "SessionStart" and pane:
        # /clear (and resume) starts a new session id in the same pane without reliably firing
        # SessionEnd for the old one; drop the old files so their stale color does not linger.
        for outro in SESSOES.glob("*.json"):
            if outro != arq:
                try:
                    if json.loads(outro.read_text()).get("pane") == pane:
                        outro.unlink(missing_ok=True)
                except Exception:
                    pass
    prev = e.get("estado")
    e.update(cwd=d.get("cwd", e.get("cwd", "")), ts=agora, pane=pane)
    agents = [a for a in e.get("agents") or [] if a != d.get("agent_id")] if ev == "SubagentStop" else list(e.get("agents") or [])
    types = {k: v for k, v in (e.get("agent_types") or {}).items() if k in agents}
    if ev == "SubagentStart" and d.get("agent_id"):
        agents.append(d["agent_id"])
        types[d["agent_id"]] = d.get("agent_type") or "agent"
    e["agents"], e["agent_types"] = agents, types
    if ev in ("SubagentStart", "SubagentStop"):
        e["ts"] = agora     # running subagents are tracked in e["agents"]; the color is derived from it
        SESSOES.mkdir(parents=True, exist_ok=True)
        tmp = arq.with_suffix(f".{os.getpid()}.tmp")
        tmp.write_text(json.dumps(e))
        os.replace(tmp, arq)
        return
    if ev in ("SessionStart", "UserPromptSubmit"):
        e["agents"], e["agent_types"] = [], {}    # a new prompt/session starts clean: a lost SubagentStop cannot stick
    if ev == "SessionStart":
        e.update(estado="ocioso", ferramenta=None)
    elif ev == "Stop":      # turn ended with text in the session: red until the next prompt
        e.update(estado="pergunta", ferramenta=None, stopped=True)
    elif ev == "UserPromptSubmit":
        e.update(estado="trabalha", ferramenta=None, stopped=False)
    elif ev == "PreToolUse" and d.get("tool_name") in ("AskUserQuestion", "ExitPlanMode"):
        e.update(estado="pergunta", ferramenta=None, stopped=False)    # poll/plan approval: waiting on the person
    elif d.get("agent_id") and ev in ("PreToolUse", "PostToolUse", "PostToolUseFailure"):
        # subagent tools share the parent's session id: the session is waiting on an agent
        # (green), not on a long-running command (yellow), and must not mask a pending question.
        if e.get("estado") != "pergunta":
            e.update(estado="trabalha", ferramenta=None)
    elif ev == "PreToolUse":
        e.update(estado="trabalha", ferramenta=d.get("tool_name"), ferr_ts=agora)
    elif ev in ("PostToolUse", "PostToolUseFailure"):
        e.update(estado="trabalha", ferramenta=None)
    elif ev == "PermissionRequest" or (ev == "Notification" and d.get("notification_type")
                                       in ("permission_prompt", "elicitation_dialog")):
        e.update(estado="pergunta", stopped=False)
    elif ev == "Notification":
        return      # idle_prompt keeps whatever Stop set (red if the session has text)
    else:
        return
    if e.get("estado") == "pergunta" and prev != "pergunta":
        _notify(d, ev)      # only the transition into red: PermissionRequest + Notification fire once
    SESSOES.mkdir(parents=True, exist_ok=True)
    tmp = arq.with_suffix(f".{os.getpid()}.tmp")
    tmp.write_text(json.dumps(e))
    os.replace(tmp, arq)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass
