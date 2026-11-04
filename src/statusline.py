#!/usr/bin/env python3
"""Claude Code statusLine command: saves the account rate limits to a local file.

Claude Code pipes a JSON to stdin after each response; for claude.ai plans it carries
`rate_limits` (5-hour and 7-day windows: used_percentage, resets_at). We keep only that, in
$WS_STATE/limits.json, for statusbar.py to read. No network, no credentials. Prints nothing,
so the Claude Code status line itself stays empty.
"""
import json
import os
import sys
import time

import lib


def main():
    try:
        limits = json.load(sys.stdin).get("rate_limits")
    except Exception:
        return
    if not isinstance(limits, dict):
        return
    lib.ESTADO.mkdir(parents=True, exist_ok=True)
    path = lib.ESTADO / "limits.json"
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps({"ts": time.time(), "rate_limits": limits}))
    os.replace(tmp, path)


if __name__ == "__main__":
    main()
