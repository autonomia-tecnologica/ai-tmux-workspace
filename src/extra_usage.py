#!/usr/bin/env python3
"""Fetch the account's "extra usage" (monthly spend) and cache the numbers for statusbar.py.

Opt-in: does nothing unless `"extra_usage": true` is set in settings.json. Spawned detached by
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
import urllib.request
from pathlib import Path

import lib

URL = "https://api.anthropic.com/api/oauth/usage"   # fixed on purpose: no env/config override
TIMEOUT_S = 5
POLL_S = 300
CACHE = lib.ESTADO / "extra_usage.json"
ATTEMPT = lib.ESTADO / "extra_usage.attempt"

# UNCONFIRMED: whether `used_credits` / `monthly_limit` are dollars or cents. Must be confirmed
# against a real response. The raw values are kept in the cache, so only this constant changes.
EXTRA_USAGE_UNIT_DIVISOR = 100   # 100 = values are cents; 1 = values are already dollars


def to_dollars(raw):
    """Convert a raw endpoint amount to dollars (see EXTRA_USAGE_UNIT_DIVISOR), or None."""
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return None
    return raw / EXTRA_USAGE_UNIT_DIVISOR


def enabled():
    return lib.ler_config().get("extra_usage") is True


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None   # 3xx surfaces as an HTTPError, i.e. a failure


def build_opener():
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
    """-> dict with raw numbers, or None if the account has no usable extra usage."""
    eu = data.get("extra_usage") if isinstance(data, dict) else None
    if not isinstance(eu, dict) or eu.get("is_enabled", eu.get("enabled")) is not True:
        return None
    num = lambda v: v if isinstance(v, (int, float)) and not isinstance(v, bool) else None
    return {"used": num(eu.get("used_credits")), "limit": num(eu.get("monthly_limit")),
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
    req = urllib.request.Request(URL, method="GET", headers={
        "Authorization": f"Bearer {token}", "anthropic-beta": "oauth-2025-04-20"})
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
