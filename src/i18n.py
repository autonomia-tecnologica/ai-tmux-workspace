"""Texts shown to the end user and prompts sent to the LLM live in i18n/<locale>.json, never in code.

pt-BR is the only locale; another one is added by dropping a new file in i18n/ and changing LOCALE.
"""
import json

import lib

LOCALE = "pt-BR"
_STRINGS = json.loads((lib.BASE / "i18n" / f"{LOCALE}.json").read_text(encoding="utf-8"))


def t(key, **kw):
    return _STRINGS[key].format(**kw) if kw else _STRINGS[key]


if __name__ == "__main__":      # shell/tmux access: i18n.py <key> [name=value ...]
    import sys
    print(t(sys.argv[1], **dict(a.split("=", 1) for a in sys.argv[2:])))
