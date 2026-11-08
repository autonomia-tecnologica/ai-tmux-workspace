#!/usr/bin/env python3
"""Fetch the account's "extra usage" (monthly spend) and cache the numbers for statusbar.py.

Opt-in: does nothing unless `extra_usage: true` is set in config.yaml. Spawned detached by
statusbar.py at most once per 5 minutes; never runs as a daemon.

Security: the OAuth access token is the account key. It is read (never refreshed), sent only in
the Authorization header of one GET to a hardcoded host, and never printed, logged, put in argv,
in exception text or on disk. Redirects are refused and proxies ignored so the token cannot go
anywhere else. Every failure is silent and keeps the previous cache.

The endpoint is unofficial and undocumented: it may change or disappear without notice.
"""
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import lib

URL = "https://api.anthropic.com/api/oauth/usage"   # fixed on purpose: no env/config override
TIMEOUT_S = 5
POLL_S = 300
CACHE = lib.ESTADO / "extra_usage.json"
ATTEMPT = lib.ESTADO / "extra_usage.attempt"

def enabled():
    return lib.ler_config().get("extra_usage") is True


def build_opener():
    # urllib is imported here, not at module top: statusbar.py imports this module on every
    # refresh and only needs enabled()/CACHE, and urllib alone costs ~20 ms of import time.
    import urllib.request

    class _NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *args, **kwargs):
            return None   # 3xx surfaces as an HTTPError, i.e. a failure

    # ProxyHandler({}) ignores *_PROXY env vars; the default HTTPS handler verifies TLS.
    return urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())


def _token_from(blob):
    oauth = json.loads(blob)["claudeAiOauth"]
    exp = oauth.get("expiresAt")
    if isinstance(exp, (int, float)) and exp / 1000 < time.time():
        return None   # expired: we never refresh
    tok = oauth.get("accessToken")
    return tok if isinstance(tok, str) and tok else None


def _from_file():
    return _token_from((Path.home() / ".claude" / ".credentials.json").read_text())


def _from_keychain():
    out = subprocess.run(["security", "find-generic-password", "-s", "Claude Code-credentials", "-w"],
                         capture_output=True, text=True, timeout=10, check=True).stdout
    return _token_from(out)


def read_token():
    readers = [_from_keychain, _from_file] if sys.platform == "darwin" else [_from_file]
    for reader in readers:
        try:
            tok = reader()
            if tok:
                return tok
        except Exception:
            continue
    return None


def parse(data):
    """-> dict with amounts in major currency units, or None if extra usage is off/absent.

    Real format: extra_usage.{is_enabled, used_credits, monthly_limit, decimal_places}, amounts in
    minor units (value / 10**decimal_places). Off only if is_enabled is False or used_credits is absent.
    """
    eu = data.get("extra_usage") if isinstance(data, dict) else None
    if not isinstance(eu, dict) or eu.get("is_enabled") is False:
        return None
    num = lambda v: v if isinstance(v, (int, float)) and not isinstance(v, bool) else None
    places = eu.get("decimal_places")
    places = places if isinstance(places, int) and not isinstance(places, bool) and 0 <= places <= 6 else 2
    div = 10 ** places
    used, limit = num(eu.get("used_credits")), num(eu.get("monthly_limit"))
    if used is None:
        return None
    return {"used": used / div, "limit": None if limit is None else limit / div,
            "utilization": num(eu.get("utilization"))}


def save(entry):
    lib.ESTADO.mkdir(parents=True, exist_ok=True)
    tmp = CACHE.with_suffix(".tmp")
    tmp.write_text(json.dumps(entry))
    os.replace(tmp, CACHE)


def run(opener=None, token_reader=None):
    if not enabled():
        return
    token = (token_reader or read_token)()
    if not token:
        return
    import urllib.request
    req = urllib.request.Request(URL, method="GET", headers={
        "Authorization": f"Bearer {token}", "anthropic-beta": "oauth-2025-04-20",
        "User-Agent": "claude-code/2.1"})
    with (opener or build_opener()).open(req, timeout=TIMEOUT_S) as resp:
        if resp.status != 200:
            return
        data = json.loads(resp.read())
    entry = parse(data) or {"used": None, "limit": None, "utilization": None, "disabled": True}
    entry["fetched_at"] = time.time()
    save(entry)


def main():
    try:
        run()
    except BaseException:
        pass


if __name__ == "__main__":
    main()
