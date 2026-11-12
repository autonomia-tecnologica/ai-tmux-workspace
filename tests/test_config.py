"""Tests for the YAML config: the stdlib subset parser, reading the new YAML, the old settings.json, the
migration, sibling relations and the parent folder. Every test points AT_WS_CONFIG (or HOME) at a
temporary folder, so the real ~/.config/ai-tmux-workspace is never read or written."""
import hashlib
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

import hook  # noqa: E402
import icons  # noqa: E402
import lib  # noqa: E402
import yaml_min  # noqa: E402
from i18n import _STRINGS  # noqa: E402

def home(p):
    """Expected path of a home-relative entry, resolved the same way lib.expand_path resolves it."""
    return lib.expand_path(str(Path.home() / p))


EXAMPLE = Path(__file__).resolve().parent.parent / "config" / "config.example.yaml"


class YamlMinTest(unittest.TestCase):
    def test_example_config_loads_and_roundtrips(self):
        cfg = yaml_min.loads(EXAMPLE.read_text(encoding="utf-8"))
        self.assertIs(cfg["manage"], True)
        self.assertEqual(cfg["profiles"]["claude"], "claude")
        expected_models = {
            "opencode-claude": "anthropic/claude-sonnet-5-5",
            "opencode-deepseek": "deepseek/deepseek-flash",
            "opencode-openrouter": "openrouter/deepseek/deepseek-v4-flash-0731",
            "opencode-free": "opencode/deepseek-v4.1-flash-free",
        }
        for name, model in expected_models.items():
            self.assertEqual(cfg["profiles"][name]["harness"], "opencode")
            self.assertEqual(cfg["profiles"][name]["model"], model)
        self.assertNotIn("claude-deepseek", cfg["profiles"])
        self.assertEqual(cfg["groups"]["Example"][1]["siblings"], ["~/Codes/some-repo"])
        self.assertEqual(yaml_min.loads(yaml_min.dumps(cfg)), cfg)

    def test_scalars_comments_and_flow(self):
        doc = (
            "# comment\n"
            "main: ~/Codes/main   # trailing\n"
            "n: 3\nf: 1.5\nflag: true\nnothing: ~\n"
            "q: \"say \\\"hi\\\" # not a comment\"\n"
            "s: 'it''s'\n"
            "hash: ~/repo#1\n"
            "sib: [~/b, \"~/c d\", null]\n"
            "empty: []\nmap: {}\n"
        )
        self.assertEqual(yaml_min.loads(doc), {
            "main": "~/Codes/main", "n": 3, "f": 1.5, "flag": True, "nothing": None,
            "q": 'say "hi" # not a comment', "s": "it's", "hash": "~/repo#1",
            "sib": ["~/b", "~/c d", None], "empty": [], "map": {},
        })

    def test_sequences_of_mappings_and_nesting(self):
        doc = (
            "groups:\n"
            "  Pair:\n"
            "    - path: ~/a\n"
            "      siblings:\n"
            "        - ~/b\n"
            "    - ~/b\n"
            "items:\n"
            "- one\n"
            "- two\n"
        )
        self.assertEqual(yaml_min.loads(doc), {
            "groups": {"Pair": [{"path": "~/a", "siblings": ["~/b"]}, "~/b"]},
            "items": ["one", "two"],
        })

    def test_writer_quotes_only_what_needs_it_and_reads_back(self):
        weird = {"s": ["-x", "a: b", "#h", "x #y", " pad ", "ç ação", "", "~", "null", "yes",
                       "tab\there", "back\\slash", "1.0", "it's", "~/Codes/ok"], "n": None}
        self.assertEqual(yaml_min.loads(yaml_min.dumps(weird)), weird)
        self.assertIn("  - ~/Codes/ok", yaml_min.dumps(weird))   # plain path stays unquoted

    def test_empty_and_root_cases(self):
        self.assertIsNone(yaml_min.loads(""))
        self.assertEqual(yaml_min.loads("{}"), {})
        self.assertEqual(yaml_min.dumps({}), "{}\n")
        with self.assertRaises(TypeError):
            yaml_min.dumps(["not", "a", "mapping"])

    def test_unsupported_or_broken_input_raises_with_line(self):
        cases = {
            "a: |\n  x": 1,        # block scalar
            "a: &x 1": 1,         # anchor
            "a: 1\n---\nb: 2": 2,  # second document
            "\ta: 1": 1,          # tab indentation
            "a: 1\na: 2": 2,      # duplicate key
            "x: [1, 2": 1,        # flow sequence cut across lines
            "a: 1\n  b: 2": 2,    # bad indentation
            "{a: 1}": 1,          # flow mapping
            "just text": 1,       # not a mapping
        }
        for text, line in cases.items():
            with self.subTest(text=text):
                with self.assertRaises(yaml_min.YamlError) as ctx:
                    yaml_min.loads(text)
                self.assertEqual(ctx.exception.line, line)


class ConfigTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name).resolve()
        env = mock.patch.dict(os.environ, {"AT_WS_CONFIG": str(self.dir / "config.yaml")})
        env.start()
        self.addCleanup(env.stop)
        self.addCleanup(self._tmp.cleanup)
        lib.CONFIG_ERROR = None

    def write(self, name, text):
        path = self.dir / name
        path.write_text(text, encoding="utf-8")
        return path

    def write_legacy(self, data):
        return self.write("config.json", json.dumps(data, indent=2, ensure_ascii=False))

    def sha(self, path):
        return hashlib.sha256(path.read_bytes()).hexdigest()


class YamlConfigTest(ConfigTestCase):
    def test_reads_groups_main_and_relations_from_yaml(self):
        self.write("config.yaml", (
            "main: ~/m\n"
            "groups:\n"
            "  Cat:\n"
            "    - ~/a\n"
            "    - path: ~/b\n"
            "      siblings: [~/a]\n"
            "relations:\n"
            "  Rel:\n"
            "    - ~/a\n"
            "    - ~/b\n"
        ))
        self.assertEqual(lib.read_repos(), [("Cat", [("a", home("a")), ("b", home("b"))])])
        self.assertEqual(lib.read_main()[0], "m")
        self.assertEqual(lib.read_relations(), [("Rel", [home("a"), home("b")])])
        self.assertIsNone(lib.CONFIG_ERROR)

    def test_siblings_are_symmetric(self):
        self.write("config.yaml", "groups:\n  Cat:\n    - path: ~/a\n      siblings: [~/b]\n    - ~/b\n    - ~/c\n")
        a, b, c = (home(x) for x in "abc")
        self.assertEqual(lib.siblings(a), [b])
        self.assertEqual(lib.siblings(b), [a])   # declared only on a, visible from b
        self.assertEqual(lib.siblings(c), [])

    def test_five_repos_declared_as_mutual_siblings_see_the_other_four(self):
        names = ["ai-tmux-workspace", "at", "site", "home", "ambiente-de-trabalho"]
        lines = ["groups:", "  Autonomia:"]
        for name in names:
            others = ", ".join(str(self.dir / o) for o in names if o != name)
            lines.append(f"    - path: {self.dir / name}")
            lines.append(f"      siblings: [{others}]")
        self.write("config.yaml", "\n".join(lines) + "\n")
        for name in names:
            with self.subTest(repo=name):
                expected = sorted(str(self.dir / o) for o in names if o != name)
                self.assertEqual(lib.siblings(str(self.dir / name)), expected)

    def test_parent_folder_makes_everything_inside_siblings_and_mixes_with_declared(self):
        parent = self.dir / "AT"
        self.write("config.yaml", (
            f"main: {parent}/m\n"
            "groups:\n"
            "  Cat:\n"
            f"    - {parent}/a\n"
            f"    - {parent}/b\n"
            f"    - path: {self.dir}/outside\n"
            f"      siblings: [{parent}/a]\n"
            f"    - {self.dir}/lone\n"
            f"parent_folder: {parent}\n"
        ))
        a, b, m = (str(parent / x) for x in ("a", "b", "m"))
        outside, lone = str(self.dir / "outside"), str(self.dir / "lone")
        self.assertEqual(lib.siblings(a), sorted([b, m, outside]))   # inside: all listed inside; outside: declared
        self.assertEqual(lib.siblings(b), sorted([a, m]))
        self.assertEqual(lib.siblings(m), sorted([a, b]))
        self.assertEqual(lib.siblings(outside), [a])                 # outside the folder: only declared siblings
        self.assertEqual(lib.siblings(lone), [])

    def test_parent_folder_only_counts_repos_actually_listed(self):
        parent = self.dir / "AT"
        self.write("config.yaml", f"groups:\n  Cat:\n    - {parent}/a\nparent_folder: {parent}\n")
        self.assertEqual(lib.siblings(str(parent / "a")), [])        # b is not listed, so it is not a sibling

    def test_siblings_without_any_config(self):
        self.assertEqual(lib.siblings(str(self.dir / "x")), [])

    def test_bad_yaml_gives_empty_config_and_marks_the_error(self):
        path = self.write("config.yaml", "groups:\n  Cat:\n    - ~/a\n\tbroken: 1\n")
        self.assertEqual(lib.read_config(), {})
        self.assertEqual(lib.CONFIG_ERROR, (str(path), 4))
        self.assertEqual(lib.read_repos(), [])

    def test_set_agent_keeps_other_keys_and_refuses_to_overwrite_a_broken_file(self):
        self.write("config.yaml", "agent: claude\ngroups:\n  Cat:\n    - path: ~/a\n      siblings: [~/b]\n")
        lib.set_agent("deepseek")
        cfg = yaml_min.loads((self.dir / "config.yaml").read_text(encoding="utf-8"))
        self.assertEqual(cfg["agent"], "deepseek")
        self.assertEqual(cfg["groups"]["Cat"][0]["siblings"], ["~/b"])
        broken = self.write("config.yaml", "agent: [oops\n")
        before = self.sha(broken)
        with self.assertRaises(lib.ConfigError):
            lib.set_agent("claude")
        self.assertEqual(self.sha(broken), before)


class LegacyAndMigrationTest(ConfigTestCase):
    LEGACY = {
        "notifications": False, "agent": "claude",
        "profiles": {"claude": "claude", "deepseek": "claude --settings ~/.claude/settings.deepseek.json"},
        "prompts": ["I want to clear the session.", "Use \"quotes\" and # hash"],
        "options": [{"label": "Git status", "run": "git status -sb"}],
        "main": "~/Codes/main-repo",
        "groups": {"Example": ["~/Codes/some-repo", {"path": "~/Codes/b", "siblings": ["~/Codes/some-repo"]}]},
        "plugins": {"claude": []},
    }

    def test_reads_old_settings_json_while_yaml_does_not_exist(self):
        self.write_legacy(self.LEGACY)
        self.assertEqual(lib.read_config(), self.LEGACY)
        self.assertTrue(lib.needs_migration())
        self.assertEqual(lib.siblings(home("Codes/some-repo")), [home("Codes/b")])

    def test_migration_writes_yaml_and_never_touches_the_json(self):
        legacy = self.write_legacy(self.LEGACY)
        before = self.sha(legacy)
        destination = lib.migrate_config()
        self.assertEqual(destination, self.dir / "config.yaml")
        self.assertEqual(self.sha(legacy), before)                 # settings.json unchanged
        self.assertTrue(legacy.exists())                           # and not deleted
        self.assertEqual(yaml_min.loads(destination.read_text(encoding="utf-8")), self.LEGACY)
        self.assertEqual(lib.read_config(), self.LEGACY)            # YAML now wins
        self.assertFalse(lib.needs_migration())

    def test_migration_refuses_to_overwrite_an_existing_yaml(self):
        self.write_legacy(self.LEGACY)
        self.write("config.yaml", "agent: codex\n")
        self.assertFalse(lib.needs_migration())
        with self.assertRaises(FileExistsError):
            lib.migrate_config()

    def test_set_agent_with_only_the_old_json_writes_the_whole_config_as_yaml(self):
        legacy = self.write_legacy(self.LEGACY)
        before = self.sha(legacy)
        lib.set_agent("deepseek")
        self.assertEqual(self.sha(legacy), before)
        cfg = yaml_min.loads((self.dir / "config.yaml").read_text(encoding="utf-8"))
        self.assertEqual(cfg["agent"], "deepseek")
        self.assertEqual(cfg["prompts"], self.LEGACY["prompts"])

    def test_json_path_given_explicitly_is_read_and_written_as_json(self):
        with mock.patch.dict(os.environ, {"AT_WS_CONFIG": str(self.dir / "settings.json")}):
            (self.dir / "settings.json").write_text(json.dumps(self.LEGACY), encoding="utf-8")
            self.assertEqual(lib.read_config(), self.LEGACY)
            lib.set_agent("deepseek")
            self.assertEqual(json.loads((self.dir / "settings.json").read_text())["agent"], "deepseek")

    def test_default_paths_under_home(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("AT_WS_CONFIG", None)
            with mock.patch.object(lib, "HOME", self.dir):
                folder = self.dir / ".config" / "ai-tmux-workspace"
                folder.mkdir(parents=True)
                (folder / "settings.json").write_text(json.dumps(self.LEGACY), encoding="utf-8")
                self.assertEqual(lib.config_path(), folder / "config.yaml")
                self.assertEqual(lib.legacy_path(), folder / "settings.json")
                self.assertEqual(lib.read_config(), self.LEGACY)
                self.assertTrue(lib.needs_migration())
                self.assertEqual(lib.migrate_config(), folder / "config.yaml")


class ReadersTest(ConfigTestCase):
    def test_hook_reads_notifications_from_yaml(self):
        self.write("config.yaml", "notifications: false\n")
        self.assertFalse(hook._notifications_enabled())
        self.write("config.yaml", "notifications: true\n")
        self.assertTrue(hook._notifications_enabled())

    def test_hook_follows_the_old_json_and_defaults_to_on(self):
        self.assertTrue(hook._notifications_enabled())            # no file at all
        self.write_legacy({"notifications": False})
        self.assertFalse(hook._notifications_enabled())

    def test_icons_read_from_yaml(self):
        self.write("config.yaml", "icons: false\n")
        self.assertFalse(icons.enabled())
        self.write("config.yaml", "icons: true\n")
        self.assertTrue(icons.enabled())

    def test_new_user_strings_exist_in_pt_br(self):
        for key in ("options.migrate", "config.migrated", "config.err.migrate",
                      "config.err.read", "config.err.read_line", "config.err.write"):
            self.assertIn(key, _STRINGS)


if __name__ == "__main__":
    unittest.main()
