"""Conformidade do `security/policy.yaml` com o motor: cada classe de programa de `src/policy.py`
aparece no yaml com os mesmos programas, e cada capacidade tem a linha que a descreve.

A lista do yaml sai de linhas com formato fixo, `# programs.<classe>: a, b, c`, seguidas de
`#   Porquê: ...`. Se uma constante e a lista divergirem, o teste aponta o programa.
"""
import re
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
POLICY_YAML = ROOT / "security" / "policy.yaml"

sys.path.insert(0, str(SRC))

import policy  # noqa: E402

PROGRAM_LINE = re.compile(r"^# programs\.([a-z_]+): (.+)$", re.MULTILINE)


def described_programs(text):
    """{classe: programas} lidos das linhas `# programs.<classe>: ...` do yaml."""
    found = {}
    for name, listing in PROGRAM_LINE.findall(text):
        if name in found:
            raise AssertionError(f"classe repetida no yaml: {name}")
        found[name] = {item.strip() for item in listing.split(",") if item.strip()}
    return found


def engine_programs():
    """{classe: programas} das constantes de `src/policy.py`, lidas na hora da chamada."""
    return {
        "admin": set(policy.ADMIN_PROGRAMS),
        "net": set(policy.NET_PROGRAMS),
        "install": set(policy.INSTALL_PROGRAMS),
        "test": set(policy.TEST_PROGRAMS),
        "godot": set(policy.GODOT_PROGRAMS),
        "read": set(policy.READ_PROGRAMS),
        "write": set(policy.WRITE_PROGRAMS),
        "target_directory": set(policy.TARGET_DIRECTORY_PROGRAMS),
        "write_option": set(policy.WRITE_OPTION_PROGRAMS),
    }


def divergences(text, engine):
    """Lista de divergências entre a descrição do yaml e as classes do motor (vazia = conferem)."""
    described = described_programs(text)
    problems = []
    for name in sorted(set(engine) | set(described)):
        if name not in described:
            problems.append(f"{name}: falta `# programs.{name}:` no yaml")
        elif name not in engine:
            problems.append(f"{name}: falta a constante no motor (src/policy.py)")
        else:
            only_yaml = described[name] - engine[name]
            only_engine = engine[name] - described[name]
            if only_yaml:
                problems.append(f"{name}: só no yaml: {', '.join(sorted(only_yaml))}")
            if only_engine:
                problems.append(f"{name}: só no motor: {', '.join(sorted(only_engine))}")
    return problems


class PolicyProgramsMirrorTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.text = POLICY_YAML.read_text(encoding="utf-8")

    def test_yaml_lists_every_program_class_of_the_engine(self):
        self.assertEqual(divergences(self.text, engine_programs()), [])

    def test_a_program_removed_on_either_side_fails(self):
        without_in_yaml = self.text.replace("sudo, doas, pkexec, su,", "doas, pkexec, su,", 1)
        self.assertNotEqual(without_in_yaml, self.text)
        self.assertTrue(divergences(without_in_yaml, engine_programs()))

        without_in_engine = policy.NET_PROGRAMS - {"gh"}
        with mock.patch.object(policy, "NET_PROGRAMS", without_in_engine):
            problems = divergences(self.text, engine_programs())
        self.assertEqual(problems, ["net: só no yaml: gh"])

    def test_each_program_class_has_a_reason_line(self):
        for name in engine_programs():
            with self.subTest(classe=name):
                pattern = re.compile(
                    rf"^# programs\.{re.escape(name)}: .+\n#   Porquê: \S", re.MULTILINE)
                self.assertRegex(self.text, pattern)

    def test_each_capability_has_a_description_line(self):
        for capability in sorted(policy.CAPABILITIES):
            with self.subTest(capacidade=capability):
                self.assertIn(f"# capacidade {capability}: ", self.text)


if __name__ == "__main__":
    unittest.main()
