"""Minimal YAML reader and writer (stdlib only) for the subset the workspace config uses.

Supported: block mappings and sequences (space indentation), plain and quoted scalars ("..." with
JSON escapes, '...' with ''), true/false, null/~, integers and decimals, one-line flow sequences of
scalars ([a, "b"]), empty {} and [], and # comments. Anything else (anchors, tags, block scalars
| and >, multi-line flow, flow mappings, several documents) raises YamlError.

Writer output always reads back to the same object (lib checks this before migrating a config).
"""
import json
import re


class YamlError(ValueError):
    """Invalid config text. `line` is the 1-based line; `reason` is a short English note for logs."""

    def __init__(self, line, reason):
        super().__init__(f"line {line}: {reason}")
        self.line = line
        self.reason = reason


_INT = re.compile(r"[-+]?\d+")
_FLOAT = re.compile(r"[-+]?(\d+\.\d*|\.\d+)([eE][-+]?\d+)?")
_QUOTED_KEY = re.compile(r'"(?:[^"\\]|\\.)*"|\'(?:[^\']|\'\')*\'')
_RESERVED = "&*!|>%@`"
_NULLS = ("", "~", "null", "Null", "NULL")
_BOOLS = {"true": True, "True": True, "TRUE": True, "false": False, "False": False, "FALSE": False}


def _strip_comment(text):
    """Drops a # comment (at line start or after a space, outside quotes) and trailing spaces."""
    open_quote, i = None, 0
    while i < len(text):
        c = text[i]
        if open_quote == '"':
            if c == "\\":
                i += 2
                continue
            if c == '"':
                open_quote = None
        elif open_quote == "'":
            if c == "'":
                open_quote = None
        elif c in "\"'" and (i == 0 or text[i - 1] in " [,:-"):
            open_quote = c
        elif c == "#" and (i == 0 or text[i - 1] in " \t"):
            return text[:i].rstrip()
        i += 1
    return text.rstrip()


def _lines(text):
    """[(number, indentation, text)] of the content lines, comments and blank lines dropped."""
    out = []
    for n, raw in enumerate(text.splitlines(), 1):
        s = _strip_comment(raw)
        if not s.strip():
            continue
        lead = s[:len(s) - len(s.lstrip())]
        if "\t" in lead:
            raise YamlError(n, "tab used for indentation")
        indent = len(s) - len(s.lstrip(" "))
        if s.strip() == "---" and not out:
            continue
        if s.strip() in ("---", "...") or s.lstrip().startswith("%"):
            raise YamlError(n, "documents and directives are not supported")
        out.append([n, indent, s.strip()])
    return out


def _key(text):
    """(key, rest) of a 'key: value' line, or None when the line is not a mapping entry."""
    m = _QUOTED_KEY.match(text)
    if m:
        end = m.end()
        if text[end:end + 1] != ":" or text[end + 1:end + 2] not in ("", " "):
            return None
        key = _scalar(m.group(), 0)
    else:
        m = re.search(r":(\s|$)", text)
        if not m or text.startswith(("[", "{", "- ")) or text == "-":
            return None
        end = m.start()
        key = text[:end].strip()
        if not key:
            return None
    return key, text[end + 1:].strip()


def _flow_parts(text, n):
    """Splits the inside of a flow sequence on commas, outside quotes."""
    parts, current, open_quote, i = [], "", None, 0
    while i < len(text):
        c = text[i]
        if open_quote == '"':
            current += c
            if c == "\\" and i + 1 < len(text):
                current += text[i + 1]
                i += 2
                continue
            if c == '"':
                open_quote = None
        elif open_quote == "'":
            current += c
            if c == "'":
                open_quote = None
        elif c == ",":
            parts.append(current.strip())
            current = ""
        else:
            if c in "\"'" and not current.strip():
                open_quote = c
            current += c
        i += 1
    parts.append(current.strip())
    return parts


def _value(text, n):
    """Value after 'key:' or '- ': scalar, one-line flow sequence, or {}."""
    if text.startswith("["):
        if not text.endswith("]"):
            raise YamlError(n, "flow sequence must fit on one line")
        inside = text[1:-1].strip()
        return [] if not inside else [_scalar(p, n) for p in _flow_parts(inside, n)]
    if text == "{}":
        return {}
    if text.startswith("{"):
        raise YamlError(n, "flow mappings are not supported")
    return _scalar(text, n)


def _scalar(text, n):
    if text.startswith('"'):
        try:
            value, end = json.JSONDecoder().raw_decode(text)
        except ValueError:
            raise YamlError(n, "invalid double-quoted string")
        if text[end:].strip():
            raise YamlError(n, "text after a quoted string")
        return value
    if text.startswith("'"):
        if len(text) < 2 or not text.endswith("'"):
            raise YamlError(n, "unterminated single-quoted string")
        body = text[1:-1]
        if "'" in body.replace("''", ""):
            raise YamlError(n, "invalid single-quoted string")
        return body.replace("''", "'")
    if text[:1] in _RESERVED:
        raise YamlError(n, f"unsupported syntax: {text[0]}")
    if text in _NULLS:
        return None
    if text in _BOOLS:
        return _BOOLS[text]
    if _INT.fullmatch(text):
        return int(text)
    if _FLOAT.fullmatch(text):
        return float(text)
    return text


class _Reader:
    def __init__(self, lines):
        self.lines = lines
        self.i = 0

    def _current(self):
        return self.lines[self.i] if self.i < len(self.lines) else None

    def block(self, indent):
        n, _, text = self.lines[self.i]
        if text == "-" or text.startswith("- "):
            return self.sequence(indent)
        return self.mapping(indent)

    def child(self, indent):
        """Block under a key or a bare '-' at `indent`; None when nothing is nested there."""
        current = self._current()
        if current and current[1] > indent:
            return self.block(current[1])
        return None

    def sequence(self, indent):
        items = []
        while (current := self._current()) and current[1] == indent and (current[2] == "-" or current[2].startswith("- ")):
            n, _, text = current
            rest = text[1:].lstrip()
            if not rest:
                self.i += 1
                items.append(self.child(indent))
            elif _key(rest) is not None:
                # "- key: value": the rest of the line is a mapping whose column is the key's column
                column = indent + (len(text) - len(rest))
                self.lines[self.i] = [n, column, rest]
                items.append(self.block(column))
            else:
                self.i += 1
                items.append(_value(rest, n))
        if (current := self._current()) and current[1] > indent:
            raise YamlError(current[0], "bad indentation")
        return items

    def mapping(self, indent):
        result = {}
        while (current := self._current()) and current[1] == indent:
            n, _, text = current
            if text == "-" or text.startswith("- "):
                break
            pair = _key(text)
            if pair is None:
                raise YamlError(n, "expected 'key: value'")
            key, rest = pair
            if key in result:
                raise YamlError(n, f"duplicate key {key!r}")
            self.i += 1
            if rest:
                result[key] = _value(rest, n)
            else:
                following = self._current()
                if following and following[1] == indent and (following[2] == "-" or following[2].startswith("- ")):
                    result[key] = self.sequence(indent)      # sequence at the same column as its key
                else:
                    result[key] = self.child(indent)
        if (current := self._current()) and current[1] > indent:
            raise YamlError(current[0], "bad indentation")
        return result


def loads(text):
    """Object from YAML text; empty text gives None."""
    lines = _lines(text)
    if not lines:
        return None
    if len(lines) == 1 and lines[0][1] == 0 and lines[0][2] in ("{}", "[]"):
        return _value(lines[0][2], lines[0][0])
    if lines[0][1] != 0:
        raise YamlError(lines[0][0], "bad indentation")
    reader = _Reader(lines)
    obj = reader.block(0)
    if reader.i != len(lines):
        raise YamlError(lines[reader.i][0], "bad indentation")
    return obj


def _needs_quotes(s):
    return (s in _NULLS or s in _BOOLS or _INT.fullmatch(s) is not None or _FLOAT.fullmatch(s) is not None
            or s != s.strip() or s[0] in "\"'[]{},#&*!|>%@`?-:" or ": " in s or s.endswith(":")
            or " #" in s or any(c in s for c in "'\"\\") or any(ord(c) < 32 for c in s))


def _emit(v):
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
        return json.dumps(v, ensure_ascii=False) if _needs_quotes(v) else v
    raise TypeError(f"cannot write {type(v).__name__} as a config value")


def _inline(v):
    """Empty containers and scalars, as they sit after 'key:' or '- '."""
    if v == {} and isinstance(v, dict):
        return "{}"
    if v == [] and isinstance(v, list):
        return "[]"
    if isinstance(v, (dict, list)):
        raise TypeError("nested collections in a sequence item are not supported")
    return _emit(v)


def _output_lines(obj, indent):
    pad = " " * indent
    if isinstance(obj, dict):
        out = []
        for key, v in obj.items():
            if isinstance(v, (dict, list)) and v:
                out.append(f"{pad}{_emit(str(key))}:")
                out += _output_lines(v, indent + 2)
            else:
                out.append(f"{pad}{_emit(str(key))}: {_inline(v)}")
        return out
    out = []
    for item in obj:
        if isinstance(item, dict) and item:
            sub = _output_lines(item, indent + 2)
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
    return "\n".join(_output_lines(obj, 0)) + "\n"
