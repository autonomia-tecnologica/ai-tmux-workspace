"""Tests for src/backlog.py and the pure counter helper in src/panel_tasks.py.

Every BACKLOG.md is synthetic and written to a temporary directory; no real backlog is read.
"""
import os
import sys
import tempfile
import unittest
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src"
sys.path.insert(0, str(SRC))

import backlog  # noqa: E402


class BacklogTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    def parse(self, text):
        path = self.root / "BACKLOG.md"
        path.write_text(text, encoding="utf-8")
        items = backlog.parse(path)
        self.assertIsNotNone(items)
        return items

    @staticmethod
    def by_id(items):
        return {i["id"]: i for i in items}

    # --- parser -------------------------------------------------------------------------------
    def test_paths_parsing(self):
        items = self.parse("""Próximo id: 100

## Refinadas

### 1. One
- Caminhos:  `src/a.py`,, `src/b.py`   src/c.py  ``
- Aceite: ok
""")
        self.assertEqual(items[0]["paths"], ["src/a.py", "src/b.py", "src/c.py"])

    def test_priority_values(self):
        items = self.by_id(self.parse("""Próximo id: 100

## Refinadas

### 1. A
- Prioridade: P0

### 2. B
- Prioridade: p1

### 3. C
- Prioridade: P2

### 4. D

### 5. E
- Prioridade: urgente
"""))
        self.assertEqual(items[1]["priority"], "P0")
        self.assertEqual(items[2]["priority"], "P1")
        self.assertEqual(items[3]["priority"], "P2")
        self.assertEqual(items[4]["priority"], "P2")       # absent defaults to P2
        self.assertEqual(items[5]["priority"], "P2")       # invalid defaults to P2
        self.assertEqual(items[5]["priority_raw"], "urgente")

    def test_acceptance_regex(self):
        items = self.by_id(self.parse("""Próximo id: 100

## Refinadas

### 1. A
- Aceite (a ajustar): revisar depois

### 2. B
- Conferir: conferir isso
"""))
        self.assertEqual(items[1]["accept"], "revisar depois")
        self.assertEqual(items[2]["accept"], "conferir isso")

    # --- impact -------------------------------------------------------------------------------
    def test_unblocks_direct_and_transitive(self):
        items = self.by_id(self.parse("""Próximo id: 100

## Refinadas

### 1. A

### 2. B
- Depende de: 1

### 3. C
- Depende de: 2

### 4. D
- Depende de: 1
"""))
        self.assertEqual(items[1]["unblocks"], 3)          # 2, 3 and 4
        self.assertEqual(items[2]["unblocks"], 1)          # 3
        self.assertEqual(items[3]["unblocks"], 0)
        self.assertEqual(items[4]["unblocks"], 0)

    # --- ordering -----------------------------------------------------------------------------
    def test_priority_order_respects_dependencies(self):
        items = self.parse("""Próximo id: 100

## Refinadas

### 1. low dependency
- Prioridade: P2

### 2. high child
- Depende de: 1
- Prioridade: P0

### 3. high free
- Prioridade: P0

### 4. medium free
- Prioridade: P1
""")
        ids = [i["id"] for i in backlog.ordered(items)]
        self.assertEqual(ids, [3, 4, 1, 2])
        self.assertLess(ids.index(1), ids.index(2))        # never before its dependency

    def test_unblocks_breaks_priority_ties(self):
        items = self.parse("""Próximo id: 100

## Refinadas

### 1. base
- Prioridade: P2

### 2. free
- Prioridade: P2

### 3. child
- Depende de: 1
- Prioridade: P2
""")
        ids = [i["id"] for i in backlog.ordered(items)]
        self.assertLess(ids.index(1), ids.index(2))        # 1 unblocks 3, 2 unblocks nothing

    def test_legacy_validate_section_is_closed(self):
        # old backlogs still carry "A validar": its items are not listed, and a dependency on them is met
        doc = backlog.document(self._write("""Próximo id: 100

## Refinadas

### 1. ready high
- Categoria: X
- Depende de: 2

## A validar

### 2. delivered earlier
- Categoria: X
- Conferir: algo
"""))
        self.assertEqual([i["id"] for i in doc["items"]], [1])
        self.assertFalse(doc["items"][0]["blocked"])
        self.assertEqual(doc["legacy"], [2])
        self.assertEqual(doc["gone"], list(range(3, 100)))

    def test_backlog_without_validate_section_parses(self):
        doc = backlog.document(self._write("""Próximo id: 4

## Refinadas

### 1. A
- Categoria: X

## A refinar

### 3. C
- Categoria: X
"""))
        self.assertEqual([i["id"] for i in doc["items"]], [1, 3])
        self.assertEqual(doc["gone"], [2])                   # absent below "Próximo id": delivered by absence
        self.assertEqual(doc["legacy"], [])

    def _write(self, text):
        path = self.root / "BACKLOG.md"
        path.write_text(text, encoding="utf-8")
        return path

    def test_cycle_last(self):
        items = self.parse("""Próximo id: 100

## Refinadas

### 1. A
- Depende de: 2

### 2. B
- Depende de: 1

### 3. C
""")
        ordered = backlog.ordered(items)
        self.assertEqual([i["id"] for i in ordered], [3, 1, 2])
        self.assertTrue(ordered[1]["cycle"])
        self.assertTrue(ordered[2]["cycle"])

    # --- parallel work ------------------------------------------------------------------------
    def test_disjoint_with_prefix_overlap(self):
        self.assertTrue(backlog.disjoint(["src/a.py"], ["src/b.py"]))
        self.assertFalse(backlog.disjoint(["src/"], ["src/x.py"]))
        self.assertFalse(backlog.disjoint(["src"], ["src/x.py"]))
        self.assertTrue(backlog.disjoint(["src"], ["srcx/y.py"]))
        self.assertTrue(backlog.disjoint([], ["src/x.py"]))

    def test_parallel_ids(self):
        items = self.parse("""Próximo id: 100

## Refinadas

### 1. A
- Caminhos: `src/a.py`

### 2. B
- Caminhos: `src/b.py`

### 3. C
- Caminhos: `src/`

### 4. D

### 5. E
- Caminhos: `src/other.py`
- Depende de: 1
""")
        self.assertEqual(backlog.parallel_ids(items), [1, 2])

    # --- panel counters -----------------------------------------------------------------------
    def test_paused_gets_its_own_bucket(self):
        old = os.environ.pop("TMUX_PANE", None)             # keep the import from shelling out
        try:
            import panel_tasks
        finally:
            if old is not None:
                os.environ["TMUX_PANE"] = old
        self.assertEqual(panel_tasks.buckets(["ready", "paused", "paused", "refine"]),
                         {"ready": 1, "refine": 1, "blocked": 0, "paused": 2})
        self.assertEqual(panel_tasks.buckets(["ready", "blocked", "cycle", "paused"]),
                         {"ready": 1, "refine": 0, "blocked": 2, "paused": 1})


if __name__ == "__main__":
    unittest.main()
