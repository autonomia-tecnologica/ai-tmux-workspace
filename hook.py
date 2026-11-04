#!/usr/bin/env python3
"""Hook do Claude Code: grava o estado da sessão para a bolinha do painel de repos.
Lê o JSON do hook no stdin, escreve ~/.cache/workspace/sessoes/<sessao>.json. Nunca falha."""
import json
import os
import sys
import time
from pathlib import Path

SESSOES = Path(os.environ.get("WS_ESTADO", Path.home() / ".cache" / "workspace")) / "sessoes"


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
    e.update(cwd=d.get("cwd", e.get("cwd", "")), ts=agora)
    if ev in ("SessionStart", "Stop"):
        e.update(estado="ocioso", ferramenta=None)
    elif ev == "UserPromptSubmit":
        e.update(estado="trabalha", ferramenta=None)
    elif ev == "PreToolUse":
        e.update(estado="trabalha", ferramenta=d.get("tool_name"), ferr_ts=agora)
    elif ev in ("PostToolUse", "PostToolUseFailure"):
        e.update(estado="trabalha", ferramenta=None)
    elif ev == "PermissionRequest" or (ev == "Notification" and d.get("notification_type")
                                       in ("permission_prompt", "elicitation_dialog")):
        e.update(estado="pergunta")
    elif ev == "Notification":
        e.update(estado="ocioso", ferramenta=None) if d.get("notification_type") == "idle_prompt" else None
    else:
        return
    SESSOES.mkdir(parents=True, exist_ok=True)
    tmp = arq.with_suffix(f".{os.getpid()}.tmp")
    tmp.write_text(json.dumps(e))
    os.replace(tmp, arq)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass
