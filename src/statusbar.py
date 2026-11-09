#!/usr/bin/env python3
"""tmux status bar: selected repo • current model • Claude usage.

Usage:  statusbar.py <repo> [profile] [width] [tabs] [active] [pane]
(tmux calls this every few seconds; `pane` is the focused pane, #{pane_id})
Model and context % come from the <sid>.status.json of that pane's session (written by statusline.py);
without it, from the session transcript and, last, from the newest log in the repo folder.
Usage comes from the local Claude Code logs (~/.claude/projects), summed incrementally
by a background process — the bar only reads the cache, so it never blocks.
"""
import fcntl
import json
import locale
import os
import re
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

import i18n

import extra_usage
import lib

PROJ = lib.HOME / ".claude" / "projects"
CACHE = lib.STATE / "usage.json"
BUCKET = 600            # seconds per bucket
WINDOW_H, WINDOW_S = 5, 7 * 86400


def tokens(u):
    return (u.get("input_tokens") or 0) + (u.get("output_tokens") or 0) + \
           (u.get("cache_creation_input_tokens") or 0)


def update_usage():
    """Read only what grew since the last pass of each .jsonl and add tokens per bucket."""
    lib.STATE.mkdir(parents=True, exist_ok=True)
    lock = open(lib.STATE / "usage.lock", "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return
    try:
        c = json.loads(CACHE.read_text())
    except Exception:
        c = {"files": {}, "buckets": {}}
    now = time.time()
    seen = set()
    for f in PROJ.glob("*/*.jsonl"):
        try:
            st = f.stat()
        except OSError:
            continue
        if st.st_mtime < now - WINDOW_S:
            continue
        offset = c["files"].get(str(f), 0)
        if offset > st.st_size:
            offset = 0
        if offset == st.st_size:
            continue
        with open(f, "rb") as fh:
            fh.seek(offset)
            block = fh.read()
        end = block.rfind(b"\n") + 1
        for line in block[:end].splitlines():
            if b'"usage"' not in line:
                continue
            try:
                d = json.loads(line)
                m = d["message"]
                if not str(m.get("model", "")).startswith("claude-"):
                    continue
                key = (m.get("id"), d.get("requestId"))
                if key in seen:
                    continue
                seen.add(key)
                ts = datetime.fromisoformat(d["timestamp"].replace("Z", "+00:00")).timestamp()
                k = str(int(ts // BUCKET))
                c["buckets"][k] = c["buckets"].get(k, 0) + tokens(m["usage"])
            except Exception:
                continue
        c["files"][str(f)] = offset + end
    cutoff = int((now - WINDOW_S - 86400) // BUCKET)
    c["buckets"] = {k: v for k, v in c["buckets"].items() if int(k) >= cutoff}
    c["ts"] = now
    tmp = CACHE.with_suffix(".tmp")
    tmp.write_text(json.dumps(c))
    os.replace(tmp, CACHE)


def usage():
    try:
        c = json.loads(CACHE.read_text())
    except Exception:
        c = {"buckets": {}, "ts": 0}
    if time.time() - c.get("ts", 0) > 90:
        subprocess.Popen([sys.executable, __file__, "--update-usage"], stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, start_new_session=True)
    now = time.time()
    h5 = sum(v for k, v in c["buckets"].items() if int(k) * BUCKET >= now - WINDOW_H * 3600)
    d7 = sum(v for k, v in c["buckets"].items() if int(k) * BUCKET >= now - WINDOW_S)
    return h5, d7


LIMITS = lib.STATE / "limits.json"


def _pct_resets(w):
    """-> (used %, resets_at epoch|None) of one window, or None if it has no percentage."""
    if not isinstance(w, dict) or not isinstance(w.get("used_percentage"), (int, float)):
        return None
    r = w.get("resets_at")
    if isinstance(r, str):
        try:
            r = datetime.fromisoformat(r.replace("Z", "+00:00")).timestamp()
        except ValueError:
            r = None
    return w["used_percentage"], r if isinstance(r, (int, float)) else None




def when(ts):
    """Reset time: HH:MM if today, 'tomorrow HH:MM' if tomorrow, else 'weekday HH:MM'."""
    d, today = datetime.fromtimestamp(ts), datetime.now()
    days = (d.date() - today.date()).days
    time_text = d.strftime("%H:%M")
    if days <= 0:
        return time_text
    return i18n.t("status.tomorrow", time=time_text) if days == 1 else f"{i18n.t(f'day.{d.weekday()}')} {time_text}"


def limits():
    """Real 5h / 7d percentages saved by statusline.py -> (h5, d7) strings, or None."""
    try:
        rl = json.loads(LIMITS.read_text())["rate_limits"]
    except Exception:
        return None
    out = []
    for names in (("five_hour", "5h", "five"), ("seven_day", "7d", "seven")):
        w = next((rl[k] for k in rl if any(n in k.lower() for n in names)), None)
        v = _pct_resets(w)
        if v is None:
            out.append("—")
            continue
        pct, resets = v
        if resets and resets < time.time():                  # window already reset
            out.append("0%")
        else:
            out.append(f"{pct:.0f}%" + (f" ↻{when(resets)}" if resets else ""))
    return tuple(out)


def _fmt_amount(v, cents):
    """Locale-aware number: 2 decimals when `cents` (small amounts), else whole with grouping."""
    try:
        locale.setlocale(locale.LC_ALL, "")
    except locale.Error:
        pass
    return locale.format_string("%.2f" if cents else "%.0f", v, grouping=True)


def extra_usage_txt():
    """Extra usage from extra_usage.json -> '$used/$limit' ($12.30/$50 below 100, $1,234/$5,000 above), or None."""
    try:
        if not extra_usage.enabled():
            return None
        c = json.loads(extra_usage.CACHE.read_text())
    except Exception:
        return None
    if c.get("disabled"):
        return None
    used, limit = c.get("used"), c.get("limit")
    if not isinstance(used, (int, float)):
        return None
    txt = "$" + _fmt_amount(used, used < 100)
    if isinstance(limit, (int, float)):
        txt += "/$" + _fmt_amount(limit, limit < 100 and limit != int(limit))
    return txt


def poll_extra_usage():
    """If enabled and the last fetch/attempt is older than 5 min, spawn extra_usage.py detached."""
    try:
        if not extra_usage.enabled():
            return
        last = 0
        for f in (extra_usage.CACHE, extra_usage.ATTEMPT):
            try:
                last = max(last, f.stat().st_mtime)
            except OSError:
                pass
        if time.time() - last < extra_usage.POLL_S:
            return
        lib.STATE.mkdir(parents=True, exist_ok=True)
        extra_usage.ATTEMPT.touch()          # failed attempts also wait 5 minutes
        subprocess.Popen([sys.executable, str(Path(extra_usage.__file__))], stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    except Exception:
        pass


def fmt(n):
    if n >= 1e6:
        return f"{n / 1e6:.1f}M".replace(".", ",")
    if n >= 1e3:
        return f"{n / 1e3:.0f}k"
    return str(n)


def clean_text(value):
    """Non-blank string -> stripped string, anything else -> None."""
    if not isinstance(value, str) or not value.strip():
        return None
    return value.strip()


def as_number(value):
    """int/float (not bool) -> itself, anything else -> None."""
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def session_of_pane(pane):
    """(sid, entry) of the live session in `pane`, or (None, None)."""
    hit = lib.session_for_pane(pane)
    return (hit[0], hit[1]) if hit else (None, None)


def status_of(sid):
    """What statusline.py saved for session `sid` (model, context_pct, cost_usd, ts) -> dict, or {}."""
    if not sid:
        return {}
    try:
        data = json.loads((lib.SESSIONS / f"{sid}.status.json").read_text())
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def model_of(repo, transcript=None):
    """Model of the last answer: from the session transcript when present, else the newest repo log."""
    if clean_text(transcript):
        logs = [Path(os.path.expanduser(transcript))]
    else:
        folder = PROJ / re.sub(r"[^A-Za-z0-9]", "-", repo)
        try:
            logs = sorted(folder.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
        except OSError:
            logs = []
    for f in logs[:2]:
        try:
            with open(f, "rb") as fh:
                fh.seek(0, 2)
                fh.seek(max(0, fh.tell() - 400_000))
                tail = fh.read().decode("utf-8", "ignore")
        except OSError:
            continue
        found = re.findall(r'"model":"([^"<][^"]*)"', tail)
        if found:
            return found[-1].removeprefix("claude-")
    return "—"


def main():
    if "--update-usage" in sys.argv:
        update_usage()
        return
    repo = sys.argv[1] if len(sys.argv) > 1 else os.getcwd()
    profile = lib.agent_name()
    width = int(sys.argv[3]) if len(sys.argv) > 3 and sys.argv[3].isdigit() else 999
    if width < 100:       # phone: repo name + every Claude tab (active one highlighted)
        tabs = sys.argv[4].split() if len(sys.argv) > 4 else []
        active = sys.argv[5] if len(sys.argv) > 5 else ""
        marks = "".join(
            f" #[bg=colour208,fg=colour16,bold] {i} #[default]" if p == active else f" #[fg=colour250]{i}#[default]"
            for i, p in enumerate(tabs, 1)) if len(tabs) > 1 else ""
        print(f"#[fg=colour75,bold] {(os.path.basename(repo) or repo).replace('#', '##')}#[default]{marks}")
        return
    if not lib.is_claude_harness():
        # OpenCode keeps no Claude transcript: show the repo and the profile, not Claude model/usage.
        name = (os.path.basename(repo) or repo).replace("#", "##")
        print(f"#[fg=colour75,bold] {name}#[nobold]#[fg=colour240] • "
              f"#[fg=colour250]{i18n.t('status.opencode', profile=profile)}#[default]")
        return
    lim = limits()
    poll_extra_usage()
    usd = extra_usage_txt()
    h5, d7 = usage() if lim is None and usd is None else (0, 0)
    name = (os.path.basename(repo) or repo).replace("#", "##")
    sep = "#[fg=colour240] • "
    if lim or usd:
        parts = ([i18n.t("status.usage", h5=lim[0], d7=lim[1])] if lim else []) + ([usd] if usd else [])
        usage_text = " · ".join(parts)
    else:
        usage_text = i18n.t("status.usage_tokens", h5=fmt(h5), d7=fmt(d7))
    pane = sys.argv[6] if len(sys.argv) > 6 else ""
    sid, entry = session_of_pane(pane)
    st = status_of(sid)
    model = clean_text(st.get("model")) or model_of(repo, (entry or {}).get("transcript_path"))
    pct = as_number(st.get("context_pct"))
    ctx = f"{sep}#[fg=colour250]{i18n.t('status.context', pct=f'{pct:.0f}')}" if pct is not None else ""
    print(f"#[fg=colour75,bold] {name}#[nobold]{sep}#[fg=colour250]{model}"
          f"{(' (' + profile + ')') if profile and profile != 'claude' else ''}{ctx}{sep}"
          f"#[fg=colour250]{usage_text}#[default]")


if __name__ == "__main__":
    main()
