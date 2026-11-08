"""Minimal YAML reader and writer (stdlib only) for the subset the workspace config uses.

Supported: block mappings and sequences (space indentation), plain and quoted scalars ("..." with
JSON escapes, '...' with ''), true/false, null/~, integers and decimals, one-line flow sequences of
scalars ([a, "b"]), empty {} and [], and # comments. Anything else (anchors, tags, block scalars
| and >, multi-line flow, flow mappings, several documents) raises ErroYAML.

Writer output always reads back to the same object (lib checks this before migrating a config).
"""
import json
import re


class ErroYAML(ValueError):
    """Invalid config text. `linha` is the 1-based line; `motivo` is a short English note for logs."""

    def __init__(self, linha, motivo):
        super().__init__(f"line {linha}: {motivo}")
        self.linha = linha
        self.motivo = motivo


_INT = re.compile(r"[-+]?\d+")
_FLOAT = re.compile(r"[-+]?(\d+\.\d*|\.\d+)([eE][-+]?\d+)?")
_CHAVE_ASPAS = re.compile(r'"(?:[^"\\]|\\.)*"|\'(?:[^\']|\'\')*\'')
_RESERVADOS = "&*!|>%@`"
_NULOS = ("", "~", "null", "Null", "NULL")
_BOOLS = {"true": True, "True": True, "TRUE": True, "false": False, "False": False, "FALSE": False}


def _sem_comentario(texto):
    """Drops a # comment (at line start or after a space, outside quotes) and trailing spaces."""
    aberta, i = None, 0
    while i < len(texto):
        c = texto[i]
        if aberta == '"':
            if c == "\\":
                i += 2
                continue
            if c == '"':
                aberta = None
        elif aberta == "'":
            if c == "'":
                aberta = None
        elif c in "\"'" and (i == 0 or texto[i - 1] in " [,:-"):
            aberta = c
        elif c == "#" and (i == 0 or texto[i - 1] in " \t"):
            return texto[:i].rstrip()
        i += 1
    return texto.rstrip()


def _linhas(texto):
    """[(numero, indentacao, texto)] of the content lines, comments and blank lines dropped."""
    out = []
    for n, bruta in enumerate(texto.splitlines(), 1):
        s = _sem_comentario(bruta)
        if not s.strip():
            continue
        lead = s[:len(s) - len(s.lstrip())]
        if "\t" in lead:
            raise ErroYAML(n, "tab used for indentation")
        indent = len(s) - len(s.lstrip(" "))
        if s.strip() == "---" and not out:
            continue
        if s.strip() in ("---", "...") or s.lstrip().startswith("%"):
            raise ErroYAML(n, "documents and directives are not supported")
        out.append([n, indent, s.strip()])
    return out


def _chave(texto):
    """(chave, resto) of a 'key: value' line, or None when the line is not a mapping entry."""
    m = _CHAVE_ASPAS.match(texto)
    if m:
        fim = m.end()
        if texto[fim:fim + 1] != ":" or texto[fim + 1:fim + 2] not in ("", " "):
            return None
        chave = _escalar(m.group(), 0)
    else:
        m = re.search(r":(\s|$)", texto)
        if not m or texto.startswith(("[", "{", "- ")) or texto == "-":
            return None
        fim = m.start()
        chave = texto[:fim].strip()
        if not chave:
            return None
    return chave, texto[fim + 1:].strip()


def _partes(texto, n):
    """Splits the inside of a flow sequence on commas, outside quotes."""
    partes, atual, aberta, i = [], "", None, 0
    while i < len(texto):
        c = texto[i]
        if aberta == '"':
            atual += c
            if c == "\\" and i + 1 < len(texto):
                atual += texto[i + 1]
                i += 2
                continue
            if c == '"':
                aberta = None
        elif aberta == "'":
            atual += c
            if c == "'":
                aberta = None
        elif c == ",":
            partes.append(atual.strip())
            atual = ""
        else:
            if c in "\"'" and not atual.strip():
                aberta = c
            atual += c
        i += 1
    partes.append(atual.strip())
    return partes


def _valor(texto, n):
    """Value after 'key:' or '- ': scalar, one-line flow sequence, or {}."""
    if texto.startswith("["):
        if not texto.endswith("]"):
            raise ErroYAML(n, "flow sequence must fit on one line")
        dentro = texto[1:-1].strip()
        return [] if not dentro else [_escalar(p, n) for p in _partes(dentro, n)]
    if texto == "{}":
        return {}
    if texto.startswith("{"):
        raise ErroYAML(n, "flow mappings are not supported")
    return _escalar(texto, n)


def _escalar(texto, n):
    if texto.startswith('"'):
        try:
            valor, fim = json.JSONDecoder().raw_decode(texto)
        except ValueError:
            raise ErroYAML(n, "invalid double-quoted string")
        if texto[fim:].strip():
            raise ErroYAML(n, "text after a quoted string")
        return valor
    if texto.startswith("'"):
        if len(texto) < 2 or not texto.endswith("'"):
            raise ErroYAML(n, "unterminated single-quoted string")
        corpo = texto[1:-1]
        if "'" in corpo.replace("''", ""):
            raise ErroYAML(n, "invalid single-quoted string")
        return corpo.replace("''", "'")
    if texto[:1] in _RESERVADOS:
        raise ErroYAML(n, f"unsupported syntax: {texto[0]}")
    if texto in _NULOS:
        return None
    if texto in _BOOLS:
        return _BOOLS[texto]
    if _INT.fullmatch(texto):
        return int(texto)
    if _FLOAT.fullmatch(texto):
        return float(texto)
    return texto


class _Leitor:
    def __init__(self, linhas):
        self.linhas = linhas
        self.i = 0

    def _atual(self):
        return self.linhas[self.i] if self.i < len(self.linhas) else None

    def bloco(self, indent):
        n, _, texto = self.linhas[self.i]
        if texto == "-" or texto.startswith("- "):
            return self.lista(indent)
        return self.mapa(indent)

    def filho(self, indent):
        """Block under a key or a bare '-' at `indent`; None when nothing is nested there."""
        atual = self._atual()
        if atual and atual[1] > indent:
            return self.bloco(atual[1])
        return None

    def lista(self, indent):
        itens = []
        while (atual := self._atual()) and atual[1] == indent and (atual[2] == "-" or atual[2].startswith("- ")):
            n, _, texto = atual
            resto = texto[1:].lstrip()
            if not resto:
                self.i += 1
                itens.append(self.filho(indent))
            elif _chave(resto) is not None:
                # "- key: value": the rest of the line is a mapping whose column is the key's column
                coluna = indent + (len(texto) - len(resto))
                self.linhas[self.i] = [n, coluna, resto]
                itens.append(self.bloco(coluna))
            else:
                self.i += 1
                itens.append(_valor(resto, n))
        if (atual := self._atual()) and atual[1] > indent:
            raise ErroYAML(atual[0], "bad indentation")
        return itens

    def mapa(self, indent):
        dic = {}
        while (atual := self._atual()) and atual[1] == indent:
            n, _, texto = atual
            if texto == "-" or texto.startswith("- "):
                break
            par = _chave(texto)
            if par is None:
                raise ErroYAML(n, "expected 'key: value'")
            chave, resto = par
            if chave in dic:
                raise ErroYAML(n, f"duplicate key {chave!r}")
            self.i += 1
            if resto:
                dic[chave] = _valor(resto, n)
            else:
                prox = self._atual()
                if prox and prox[1] == indent and (prox[2] == "-" or prox[2].startswith("- ")):
                    dic[chave] = self.lista(indent)      # sequence at the same column as its key
                else:
                    dic[chave] = self.filho(indent)
        if (atual := self._atual()) and atual[1] > indent:
            raise ErroYAML(atual[0], "bad indentation")
        return dic


def loads(texto):
    """Object from YAML text; empty text gives None."""
    linhas = _linhas(texto)
    if not linhas:
        return None
    if len(linhas) == 1 and linhas[0][1] == 0 and linhas[0][2] in ("{}", "[]"):
        return _valor(linhas[0][2], linhas[0][0])
    if linhas[0][1] != 0:
        raise ErroYAML(linhas[0][0], "bad indentation")
    leitor = _Leitor(linhas)
    obj = leitor.bloco(0)
    if leitor.i != len(linhas):
        raise ErroYAML(linhas[leitor.i][0], "bad indentation")
    return obj


def _precisa_aspas(s):
    return (s in _NULOS or s in _BOOLS or _INT.fullmatch(s) is not None or _FLOAT.fullmatch(s) is not None
            or s != s.strip() or s[0] in "\"'[]{},#&*!|>%@`?-:" or ": " in s or s.endswith(":")
            or " #" in s or any(c in s for c in "'\"\\") or any(ord(c) < 32 for c in s))


def _emitir(v):
    """Scalar as YAML text. Strings are quoted only when a plain scalar would read back differently."""
    if v is None:
        return "null"
    if v is True:
        return "true"
    if v is False:
        return "false"
    if isinstance(v, int):
        return str(v)
    if isinstance(v, float):
        return repr(v)
    if isinstance(v, str):
        return json.dumps(v, ensure_ascii=False) if _precisa_aspas(v) else v
    raise TypeError(f"cannot write {type(v).__name__} as a config value")


def _inline(v):
    """Empty containers and scalars, as they sit after 'key:' or '- '."""
    if v == {} and isinstance(v, dict):
        return "{}"
    if v == [] and isinstance(v, list):
        return "[]"
    if isinstance(v, (dict, list)):
        raise TypeError("nested collections in a sequence item are not supported")
    return _emitir(v)


def _linhas_saida(obj, indent):
    pad = " " * indent
    if isinstance(obj, dict):
        out = []
        for chave, v in obj.items():
            if isinstance(v, (dict, list)) and v:
                out.append(f"{pad}{_emitir(str(chave))}:")
                out += _linhas_saida(v, indent + 2)
            else:
                out.append(f"{pad}{_emitir(str(chave))}: {_inline(v)}")
        return out
    out = []
    for item in obj:
        if isinstance(item, dict) and item:
            sub = _linhas_saida(item, indent + 2)
            out.append(f"{pad}- {sub[0][indent + 2:]}")
            out += sub[1:]
        else:
            out.append(f"{pad}- {_inline(item)}")
    return out


def dumps(obj):
    """YAML text of a dict (block style, keys in insertion order)."""
    if not isinstance(obj, dict):
        raise TypeError("the config root must be a mapping")
    if not obj:
        return "{}\n"
    return "\n".join(_linhas_saida(obj, 0)) + "\n"
