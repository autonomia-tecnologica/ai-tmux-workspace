#!/usr/bin/env python3
"""Open/closed state shared by the panels that show expandable rows (repos, tasks, files tree, options).

Expander keeps the set of keys whose state differs from the default (`default_open`), so a JSON file
written with `path` keeps the same format the repos panel always used (its list of collapsed groups).
With `path`, the file is read on every call: several windows share it and see each other's changes.

Left handling (`handle_arrow`): an open folder closes; otherwise the cursor moves to the parent, given
by the caller as `parent(key) -> key | None`. Panels without a parent concept pass no `parent`.
"""
import json
import pathlib


class Expander:
    def __init__(self, path=None, default_open=False):
        self.path = pathlib.Path(path) if path else None
        self.default_open = default_open
        self._marked = set()     # keys whose state differs from the default (memory only, without path)

    def _read(self):
        if self.path is None:
            return set(self._marked)
        try:
            return set(json.loads(self.path.read_text()))
        except Exception:
            return set()

    def is_open(self, key):
        return (key in self._read()) != self.default_open

    def _set(self, key, opened):
        marked = self._read()
        mark = opened != self.default_open
        if (key in marked) == mark:
            return
        (marked.add if mark else marked.discard)(key)
        if self.path is None:
            self._marked = marked
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(sorted(marked)))

    def open(self, key):
        self._set(key, True)

    def close(self, key):
        self._set(key, False)

    def toggle(self, key):
        self._set(key, not self.is_open(key))

    def handle_arrow(self, k, key, parent=None, folder=True):
        """Applies an arrow key to the row `key` and returns the key the cursor should be on.

        "right" opens a folder (`folder`). "left" closes an open folder; anything else moves to
        `parent(key)` when there is one. Rows that are not folders (`folder=False`) only move up.
        """
        if key is None:
            return key
        if k == "right":
            if folder:
                self.open(key)
            return key
        if k == "left":
            if folder and self.is_open(key):
                self.close(key)
                return key
            if parent is not None:
                return parent(key) or key
        return key
