"""Texts shown to the end user and prompts sent to the LLM live in i18n/<locale>.json, never in code.

pt-BR is the only locale; another one is added by dropping a new file in i18n/ and changing LOCALE.

Kept light on purpose: tmux.conf runs this once per load (`--tmux`), not on every status refresh.
"""
import json
from pathlib import Path

LOCALE = "pt-BR"
_BASE = Path(__file__).resolve().parent.parent          # repo root
_STRINGS = json.loads((_BASE / "i18n" / f"{LOCALE}.json").read_text(encoding="utf-8"))


def t(key, **kw):
    return _STRINGS[key].format(**kw) if kw else _STRINGS[key]


def _tmux_options():
    """tmux.* texts -> user option names: tmux.hint_nav -> @t_hint_nav (read as #{@t_hint_nav})."""
    return {"@t_" + k[len("tmux."):].replace(".", "_"): v for k, v in _STRINGS.items() if k.startswith("tmux.")}


def _apply_tmux():
    """Sets the tmux.* texts as global user options. Run by tmux.conf with run-shell, before the bars use them."""
    import tmux
    for name, value in _tmux_options().items():
        tmux.run("set", "-g", name, value)


if __name__ == "__main__":
    import sys
    if sys.argv[1:] == ["--tmux"]:     # tmux.conf: run-shell "python3 .../i18n.py --tmux"
        _apply_tmux()
    else:                              # shell/tmux access: i18n.py <key> [name=value ...]
        print(t(sys.argv[1], **dict(a.split("=", 1) for a in sys.argv[2:])))
