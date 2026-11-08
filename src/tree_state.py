#!/usr/bin/env python3
"""Open/closed state shared by the panels that show expandable rows (repos, tasks, files tree, options).

Expander keeps the set of keys whose state differs from the default (`padrao_aberto`), so a JSON file
written with `path` keeps the same format the repos panel always used (its list of collapsed groups).
With `path`, the file is read on every call: several windows share it and see each other's changes.

Left handling (`handle_arrow`): an open folder closes; otherwise the cursor moves to the parent, given
by the caller as `pai(chave) -> key | None`. Panels without a parent concept pass no `pai`.
"""
import json
import pathlib


class Expander:
    def __init__(self, path=None, padrao_aberto=False):
        self.path = pathlib.Path(path) if path else None
        self.padrao_aberto = padrao_aberto
        self._marcados = set()     # keys whose state differs from the default (memory only, without path)

    def _lidos(self):
        if self.path is None:
            return set(self._marcados)
        try:
            return set(json.loads(self.path.read_text()))
        except Exception:
            return set()

    def is_open(self, key):
        return (key in self._lidos()) != self.padrao_aberto

    def _definir(self, key, aberto):
        marcados = self._lidos()
        marcar = aberto != self.padrao_aberto
        if (key in marcados) == marcar:
            return
        (marcados.add if marcar else marcados.discard)(key)
        if self.path is None:
            self._marcados = marcados
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(sorted(marcados)))

    def open(self, key):
        self._definir(key, True)

    def close(self, key):
        self._definir(key, False)

    def toggle(self, key):
        self._definir(key, not self.is_open(key))

    def handle_arrow(self, k, chave, pai=None, pasta=True):
        """Applies an arrow key to the row `chave` and returns the key the cursor should be on.

        "right" opens a folder (`pasta`). "left" closes an open folder; anything else moves to
        `pai(chave)` when there is one. Rows that are not folders (`pasta=False`) only move up.
        """
        if chave is None:
            return chave
        if k == "right":
            if pasta:
                self.open(chave)
            return chave
        if k == "left":
            if pasta and self.is_open(chave):
                self.close(chave)
                return chave
            if pai is not None:
                return pai(chave) or chave
        return chave
