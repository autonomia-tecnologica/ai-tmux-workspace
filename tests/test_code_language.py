"""Guard: code identifiers stay in English.

The workspace keeps user-facing text in i18n/ and the BACKLOG.md labels as they are, but every
identifier (function, class, method, argument, variable, attribute, constant, exception) in
src/, harness/, security/, demo/ and tests/, and every function name in bin/workspace, must be English. This test
splits each identifier on `_` and camelCase and rejects a Portuguese segment.

Deliberate external contracts are strings, not identifiers (forcado.yaml keys such as `tipo` and
`texto`, BACKLOG.md labels), so they are outside this check.
"""
import ast
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PYTHON_DIRS = ("src", "harness", "security", "demo", "tests")

# Portuguese content words that must not appear as an identifier segment. Ambiguous English words
# that happen to exist in Portuguese too (data, item, total, base, config, ...) are deliberately
# absent, so the guard never rejects legitimate English code.
PORTUGUESE = set("""
arquivo arquivos pasta pastas caminho caminhos grupo grupos sessao sessoes aba abas tarefa tarefas
painel paineis barra linha linhas coluna colunas tecla teclas comando comandos saida entrada usuario
servidor fila itens dado dados valor valores chave chaves lista listas mapa mapas conjunto tipo tipos
modo modos nivel niveis tempo hora horas dia dias semana mes ano minuto segundo maior menor igual
diferente vazio cheio novo nova velho primeiro ultimo proximo anterior atual parte partes todo todos
toda todas cada outro outra mesmo mesma proprio propria perfil perfis estado estados ferramenta
ferramentas erro erros ocioso trabalha espera pergunta papel papeis direita esquerda ativo ativa
janela janelas quando usado depois antes dentro fora sobre subir descer volta voltar fica ficar vira
virar roda rodar usa leva levar pega pegar poe colocar fala falar diz dizer mostra mostrar faz fazer
esta estao nao sao voce tambem ja apos ate codigo funcao opcao mensagem titulo numero possivel
pequeno enquanto porque cabecalho rodape tela cor cores ler escrever abrir fechar criar remover
carregar salvar gravar obter listar esconder atualizar iniciar parar mover trocar limpar copiar colar
enviar receber esperar aguardar contar calcular verificar checar validar normalizar formatar traduzir
sujo sujos pior seta ramo letra ruim tolerar rnome usados custo primeira ultima marcado ultimo
necessita textos relacoes vida vivos ignorada ignorar resto prox dic bruta filho estilo lins pronto
caixa tam alt foco acordar recoletar coletar corpo cabeca indice sobrepor visivel rolar executar
processar marcar motivo leitor escalar emitir precisa aspas colapsado colapsados expande expandida
fechado fechada aberta aberto abertas abertos seleciona selecionado perde acorda recolhe desenha
sobrepoe rola executa processa teclado clique direito esquerdo cima baixo canto meio lado lados inicio
passo bloco larg nomes inicial pergunta tela forcado legado aviso rotulo arvore alvo padrao padroes
regra regras
""".split())

CAMEL = re.compile(r"[A-Z]+(?=[A-Z][a-z])|[A-Z]?[a-z]+|[A-Z]+|[0-9]+")


def segments(name):
    """Split an identifier on underscores and camelCase."""
    out = []
    for part in name.split("_"):
        if part:
            out.extend(match.group() for match in CAMEL.finditer(part))
    return out


def portuguese_segments(name):
    """Portuguese segments found in an identifier, in order (empty for an English identifier)."""
    return [segment for segment in segments(name) if segment.lower() in PORTUGUESE]


def python_identifiers(path):
    """Every identifier defined or referenced in a Python file."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            yield node.name, node.lineno
        elif isinstance(node, ast.Name):
            yield node.id, node.lineno
        elif isinstance(node, ast.Attribute):
            yield node.attr, node.lineno
        elif isinstance(node, ast.arg):
            yield node.arg, node.lineno
        elif isinstance(node, (ast.Global, ast.Nonlocal)):
            for name in node.names:
                yield name, node.lineno


def bin_function_names(path):
    """Function names of the bash launcher."""
    text = path.read_text(encoding="utf-8")
    for match in re.finditer(r"^\s*(?:function\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*\(\)\s*\{", text, re.M):
        yield match.group(1)


def scan_python(path):
    offenders = []
    for name, line in python_identifiers(path):
        bad = portuguese_segments(name)
        if bad:
            offenders.append(f"{path.relative_to(ROOT)}:{line}: {name} ({', '.join(bad)})")
    return offenders


def scan_bin(path):
    offenders = []
    for name in bin_function_names(path):
        bad = portuguese_segments(name)
        if bad:
            offenders.append(f"{path.relative_to(ROOT)}: {name} ({', '.join(bad)})")
    return offenders


def scan_tree():
    offenders = []
    for directory in PYTHON_DIRS:
        for path in sorted((ROOT / directory).glob("*.py")):
            offenders.extend(scan_python(path))
    offenders.extend(scan_bin(ROOT / "bin" / "workspace"))
    return offenders


class CodeLanguageTest(unittest.TestCase):
    def test_rejects_portuguese_identifier(self):
        self.assertEqual(portuguese_segments("ler_config"), ["ler"])
        self.assertEqual(portuguese_segments("perfilCommand"), ["perfil"])

    def test_accepts_english_identifier(self):
        self.assertEqual(portuguese_segments("read_config"), [])
        self.assertEqual(portuguese_segments("profileCommand"), [])
        self.assertEqual(portuguese_segments("state"), [])

    def test_source_tree_has_no_portuguese_identifiers(self):
        self.assertEqual(scan_tree(), [])


if __name__ == "__main__":
    unittest.main()
