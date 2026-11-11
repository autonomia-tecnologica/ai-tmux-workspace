"""Agent adapters behind one ACP-shaped interface.

`adapter_for(profile)` is the only entry point the workspace needs: the window profile chooses the
harness, and the caller receives the same `initialize`/`session`/`prompt`/`permission`/`update`
surface for Claude Code or OpenCode. The package does not depend on ACP; it only imitates its shape.
"""
from .base import (
    HARNESS_CLAUDE,
    HARNESS_OPENCODE,
    METHODS,
    PROTOCOL,
    Adapter,
    Prompt,
    Session,
    Update,
    central_decision,
    normalize_event,
    read_stream as _read_stream,
)
from .claude import ALLOWED_TOOLS, DISALLOWED_TOOLS, ClaudeAdapter
from .opencode import OpenCodeAdapter

CLAUDE = HARNESS_CLAUDE
OPENCODE = HARNESS_OPENCODE

ADAPTERS = {
    HARNESS_CLAUDE: ClaudeAdapter,
    HARNESS_OPENCODE: OpenCodeAdapter,
}


def adapter(harness, profile=None):
    """Adapter for a harness name; an unknown name falls back to Claude, like `profile_harness`."""
    name = str(harness or "").strip()
    return ADAPTERS.get(name, ClaudeAdapter)(profile)


def adapter_for(profile=None):
    """Adapter of a profile, resolved by `lib.profile_harness` (the window profile decides)."""
    import lib

    return adapter(lib.profile_harness(profile), profile)


def read_stream(stream, repo="", on_status=None, profile=None, selected=None):
    """Final text of a stream. `profile` names the adapter; without it the event shape decides."""
    chosen = selected
    if chosen is None and profile:
        chosen = adapter_for(profile)
    return _read_stream(stream, repo, on_status, adapter=chosen)


__all__ = [
    "ADAPTERS", "CLAUDE", "HARNESS_CLAUDE", "HARNESS_OPENCODE", "METHODS", "OPENCODE",
    "PROTOCOL", "Adapter", "ClaudeAdapter", "OpenCodeAdapter", "Prompt", "Session", "Update",
    "adapter", "adapter_for", "central_decision", "normalize_event", "read_stream",
    "ALLOWED_TOOLS", "DISALLOWED_TOOLS",
]
