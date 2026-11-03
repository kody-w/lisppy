#!/usr/bin/env python3
"""LisPy: the reader, evaluator, value wire, embedding API, and CLI.

Optional hosting lives in lispy_hosted.py; its original imports and CLI flags
remain available lazily through this module.

    python3 lisp.py script.lispy
    python3 lisp.py -e '(+ 1 2 3)'
    python3 lisp.py --teach
"""

from __future__ import annotations

import argparse
import bisect
import hashlib
import json
import math
import os
import pkgutil
import re
import struct
import sys
from pathlib import Path
from typing import Any

if __name__ == "__main__":
    sys.modules["lisp"] = sys.modules[__name__]
    sys.path.insert(0, str(Path(__file__).resolve().parent))

# Configuration

VERSION = "0.26.0"
LANGUAGE_PROFILE = "lispy-core@1"
DISTRIBUTION_NAME = "rappterbook-lispy-runtime"
REPO_ROOT = Path(__file__).resolve().parent
STDLIB_PATH = REPO_ROOT / "stdlib.lisp"
CORE_STDLIB_PATH = REPO_ROOT / "core-stdlib.lisp"
MAX_STATE_BYTES = 8_388_608
WORKER_JSON_MAX_DEPTH = 64
WORKER_JSON_MAX_NODES = 100_000

def _resource_bytes(name: str, fallback: Path) -> bytes:
    try:
        packaged = pkgutil.get_data("lisppy", f"data/{name}")
    except (ImportError, ModuleNotFoundError):
        packaged = None
    if packaged is not None:
        return packaged
    if fallback.exists():
        return fallback.read_bytes()
    raise LispError(f"missing runtime resource: {name}")

def _resource_text(name: str, fallback: Path) -> str:
    try:
        return _resource_bytes(name, fallback).decode("utf-8")
    except UnicodeDecodeError as exc:
        raise InvalidDataError(f"runtime resource is not UTF-8: {name}: {exc}")

STATE_DIR = Path(os.environ.get("STATE_DIR", str(Path.cwd() / "state"))).expanduser().resolve()

# Types

class Symbol(str):
    """A Lisp symbol — just a string that prints without quotes."""

    pass

class Nil:
    """The empty list / false value."""

    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __repr__(self):
        return "()"

    def __bool__(self):
        return False

    def __iter__(self):
        return iter([])

    def __len__(self):
        return 0

NIL = Nil()

class Pair:
    """A cons cell — the building block of lists."""

    def __init__(self, car: Any, cdr: Any):
        self.car = car
        self.cdr = cdr

    def __repr__(self):
        return "(" + _pair_repr(self) + ")"

    def __iter__(self):
        cur = self
        while isinstance(cur, Pair):
            yield cur.car
            cur = cur.cdr
        if cur is not NIL:
            yield cur  # improper list tail

    def __len__(self):
        n = 0
        cur = self
        while isinstance(cur, Pair):
            n += 1
            cur = cur.cdr
        return n

class Parameters:
    """Required parameters plus an optional dotted rest parameter."""

    def __init__(self, required: list[str], rest: str | None = None):
        self.required = required
        self.rest = rest

class ExecutionLimits:
    """Deterministic in-process limits for one LisPy execution."""

    def __init__(
        self,
        *,
        max_steps: int | None = 100_000,
        max_call_depth: int | None = 128,
        max_reader_depth: int | None = 256,
        max_source_bytes: int | None = 1_048_576,
        max_collection_items: int | None = 100_000,
        max_output_bytes: int | None = 1_048_576,
    ):
        self.max_steps = max_steps
        self.max_call_depth = max_call_depth
        self.max_reader_depth = max_reader_depth
        self.max_source_bytes = max_source_bytes
        self.max_collection_items = max_collection_items
        self.max_output_bytes = max_output_bytes

    @classmethod
    def unlimited(cls):
        return cls(
            max_steps=None,
            max_call_depth=None,
            max_reader_depth=None,
            max_source_bytes=None,
            max_collection_items=None,
            max_output_bytes=None,
        )

    def as_dict(self):
        return {
            "max_steps": self.max_steps,
            "max_call_depth": self.max_call_depth,
            "max_reader_depth": self.max_reader_depth,
            "max_source_bytes": self.max_source_bytes,
            "max_collection_items": self.max_collection_items,
            "max_output_bytes": self.max_output_bytes,
        }

class ExecutionContext:
    """Mutable counters shared by every environment in an execution."""

    def __init__(self, limits: ExecutionLimits | None = None):
        self.limits = limits or ExecutionLimits()
        self.steps = 0
        self.call_depth = 0
        self.peak_call_depth = 0

    def reset(self):
        self.steps = 0
        self.call_depth = 0
        self.peak_call_depth = 0

    def consume(self, count: int = 1):
        self.steps += count
        limit = self.limits.max_steps
        if limit is not None and self.steps > limit:
            raise ExecutionLimitExceeded("steps", limit, self.steps)

    def enter_call(self):
        self.call_depth += 1
        self.peak_call_depth = max(self.peak_call_depth, self.call_depth)
        limit = self.limits.max_call_depth
        if limit is not None and self.call_depth > limit:
            self.call_depth -= 1
            raise ExecutionLimitExceeded("call_depth", limit, limit + 1)

    def exit_call(self):
        self.call_depth -= 1

    def check_source(self, source: str):
        size = len(source.encode("utf-8"))
        limit = self.limits.max_source_bytes
        if limit is not None and size > limit:
            raise ExecutionLimitExceeded("source_bytes", limit, size)

    def check_collection(self, size: int):
        limit = self.limits.max_collection_items
        if limit is not None and size > limit:
            raise ExecutionLimitExceeded("collection_items", limit, size)

class BoundedOutput:
    """UTF-8 byte-bounded text sink used by embedded and worker runs."""

    def __init__(self, max_bytes: int | None):
        self.max_bytes = max_bytes
        self.bytes_written = 0
        self.parts = []

    def write(self, text: str):
        text = str(text)
        size = len(text.encode("utf-8"))
        used = self.bytes_written + size
        if self.max_bytes is not None and used > self.max_bytes:
            raise ExecutionLimitExceeded("output_bytes", self.max_bytes, used)
        self.parts.append(text)
        self.bytes_written = used

    __call__ = write

    def reset(self):
        self.bytes_written = 0
        self.parts = []

    def getvalue(self):
        return "".join(self.parts)

class BoundedWriter:
    """Byte-bounded forwarding writer for CLI and REPL output."""

    def __init__(self, target, max_bytes):
        self.target = target
        self.max_bytes = max_bytes
        self.bytes_written = 0

    def __call__(self, text):
        text = str(text)
        size = len(text.encode("utf-8"))
        used = self.bytes_written + size
        if self.max_bytes is not None and used > self.max_bytes:
            raise ExecutionLimitExceeded("output_bytes", self.max_bytes, used)
        self.target(text)
        self.bytes_written = used

    def reset(self):
        self.bytes_written = 0

def _pair_repr(p: Pair) -> str:
    parts = []
    cur = p
    while isinstance(cur, Pair):
        parts.append(_value_repr(cur.car, _validated=True))
        cur = cur.cdr
    if cur is not NIL:
        parts.append(".")
        parts.append(_value_repr(cur, _validated=True))
    return " ".join(parts)

class Lambda:
    """A user-defined function (closure)."""

    def __init__(self, params: Any, body: list, env: Env, name: str = "lambda"):
        self.params = params
        self.body = body
        self.env = env
        self.name = name

    def __repr__(self):
        return f"#<procedure {self.name}>"

class Macro:
    """A syntactic macro."""

    def __init__(self, params: Any, body: list, env: Env, name: str = "macro"):
        self.params = params
        self.body = body
        self.env = env
        self.name = name

    def __repr__(self):
        return f"#<macro {self.name}>"

# Environment

class Env(dict):
    """An environment frame with lexical scoping."""

    def __init__(self, params=(), args=(), outer=None, *, context: ExecutionContext | None = None, writer=None):
        super().__init__()
        self.context = context or (outer.context if outer is not None else ExecutionContext())
        self.value_ops = outer.value_ops if outer is not None else CORE1_VALUES
        self.writer = writer or (outer.writer if outer is not None else sys.stdout.write)
        self.root = outer.root if outer is not None else self
        if outer is None:
            self.readonly = set()
            self.protected_definitions = set()
            self.writable_outputs = set()
            self.tracked_writes = set()
        else:
            self.readonly = set()
            self.protected_definitions = set()
        if isinstance(params, str):
            # variadic: (lambda args body)
            self[params] = py_to_lisp(list(args))
        elif isinstance(params, Parameters):
            required = params.required
            if len(args) < len(required):
                raise LispError(f"expected at least {len(required)} args, got {len(args)}")
            if params.rest is None and len(args) != len(required):
                raise LispError(f"expected {len(required)} args, got {len(args)}")
            self.update(zip(required, args[: len(required)]))
            if params.rest is not None:
                self[params.rest] = py_to_lisp(list(args[len(required) :]))
        else:
            if len(params) != len(args):
                raise LispError(f"expected {len(params)} args, got {len(args)}")
            self.update(zip(params, args))
        self.outer = outer

    def find(self, name: str) -> Env:
        if name in self:
            return self
        if self.outer is not None:
            return self.outer.find(name)
        raise LispError(f"unbound variable: {name}")

    def define(self, name: str, value: Any) -> None:
        self.ensure_definable(name)
        self[name] = value

    def ensure_definable(self, name: str) -> None:
        if name in self.protected_definitions:
            raise CapabilityDenied(f"read-only binding: {name}")

    def assign(self, name: str, value: Any) -> None:
        self.ensure_assignable(name)
        self[name] = value
        if self is self.root and name in self.root.writable_outputs:
            self.root.tracked_writes.add(name)

    def ensure_assignable(self, name: str) -> None:
        if name in self.readonly:
            raise CapabilityDenied(f"read-only binding: {name}")

# Errors

class LispError(Exception):
    category = "evaluation"

class LispSyntaxError(LispError):
    category = "syntax"

    def __init__(self, message, *, source_name="<string>", line=1, column=1, opener=None, incomplete=False):
        self.source_name = source_name
        self.line = line
        self.column = column
        self.opener = opener
        self.incomplete = incomplete
        super().__init__(f"{source_name}:{line}:{column}: {message}")

class CapabilityDenied(LispError):
    category = "capability"

class ExecutionLimitExceeded(LispError):
    category = "resource-limit"

    def __init__(self, resource: str, limit: int, used: int):
        self.resource = resource
        self.limit = limit
        self.used = used
        super().__init__(f"execution limit exceeded: {resource} (limit {limit}, used {used})")

class UnsupportedFormError(LispError):
    category = "unsupported"

class InvalidDataError(LispError):
    category = "invalid-data"

class WireEncodingError(LispError):
    category = "serialization"

class Token(str):
    """String-compatible token carrying a source start position."""

    def __new__(cls, text, source_name, line, column, offset):
        value = str.__new__(cls, text)
        value.source_name = source_name
        value.line = line
        value.column = column
        value.offset = offset
        return value

class ReadState:
    COMPLETE = "complete"
    INCOMPLETE = "incomplete"
    INVALID = "invalid"

class ReadResult:
    def __init__(self, state, expressions=None, error=None):
        self.state = state
        self.expressions = expressions
        self.error = error

# Tokenizer & Parser

def _source_position(source, line_starts, offset):
    line_index = bisect.bisect_right(line_starts, offset) - 1
    return line_index + 1, offset - line_starts[line_index] + 1

def tokenize(source: str, *, source_name: str = "<string>") -> list[str]:
    """Break source into tokens."""
    tokens = []
    line_starts = [0]
    index = 0
    while index < len(source):
        if source[index] == "\r":
            index += 2 if index + 1 < len(source) and source[index + 1] == "\n" else 1
            line_starts.append(index)
            continue
        if source[index] == "\n":
            line_starts.append(index + 1)
        index += 1

    def token(text, offset):
        line, column = _source_position(source, line_starts, offset)
        return Token(text, source_name, line, column, offset)

    i = 0
    while i < len(source):
        c = source[i]

        if c in " \t\n\r":
            i += 1
            continue

        if c == ";":
            while i < len(source) and source[i] != "\n":
                i += 1
            continue

        if c in "()[]'`,":
            tokens.append(token(c, i))
            i += 1
            continue

        if c == '"':
            j = i + 1
            while j < len(source):
                if source[j] == "\\" and j + 1 < len(source):
                    j += 2
                    continue
                if source[j] == '"':
                    break
                j += 1
            if j >= len(source):
                line, column = _source_position(source, line_starts, i)
                raise LispSyntaxError(
                    "unterminated string", source_name=source_name, line=line, column=column, incomplete=True
                )
            tokens.append(token(source[i : j + 1], i))
            i = j + 1
            continue

        j = i
        while j < len(source) and source[j] not in " \t\n\r()[]\";,'`":
            j += 1
        tokens.append(token(source[i:j], i))
        i = j

    return tokens

def _parse_tokens(tokens, *, source_name, source_length, max_depth):
    expressions = []
    pos = 0

    def read_expr(depth=0):
        nonlocal pos
        if max_depth is not None and depth > max_depth:
            raise ExecutionLimitExceeded("reader_depth", max_depth, depth)
        if pos >= len(tokens):
            raise LispSyntaxError(
                "unexpected end of input", source_name=source_name, column=source_length + 1, incomplete=True
            )

        tok = tokens[pos]

        if tok == "'":
            pos += 1
            if pos >= len(tokens):
                raise LispSyntaxError(
                    "reader prefix requires an expression",
                    source_name=tok.source_name,
                    line=tok.line,
                    column=tok.column,
                    incomplete=True,
                )
            return [Symbol("quote"), read_expr(depth + 1)]

        if tok == "`":
            pos += 1
            if pos >= len(tokens):
                raise LispSyntaxError(
                    "reader prefix requires an expression",
                    source_name=tok.source_name,
                    line=tok.line,
                    column=tok.column,
                    incomplete=True,
                )
            return [Symbol("quasiquote"), read_expr(depth + 1)]

        if tok == ",":
            pos += 1
            if pos >= len(tokens):
                raise LispSyntaxError(
                    "reader prefix requires an expression",
                    source_name=tok.source_name,
                    line=tok.line,
                    column=tok.column,
                    incomplete=True,
                )
            return [Symbol("unquote"), read_expr(depth + 1)]

        if tok in ("(", "["):
            pos += 1
            close = ")" if tok == "(" else "]"
            lst = []
            while pos < len(tokens) and tokens[pos] != close:
                if tokens[pos] in (")", "]"):
                    actual = tokens[pos]
                    raise LispSyntaxError(
                        f"mismatched closing delimiter '{actual}'; "
                        f"expected '{close}' for '{tok}' opened at "
                        f"{tok.line}:{tok.column}",
                        source_name=actual.source_name,
                        line=actual.line,
                        column=actual.column,
                        opener=tok,
                    )
                lst.append(read_expr(depth + 1))
            if pos >= len(tokens):
                raise LispSyntaxError(
                    f"missing closing '{close}' for '{tok}' opened at {tok.line}:{tok.column}",
                    source_name=tok.source_name,
                    line=tok.line,
                    column=tok.column,
                    opener=tok,
                    incomplete=True,
                )
            pos += 1  # skip closing paren
            return lst

        if tok in (")", "]"):
            raise LispSyntaxError(
                f"unexpected closing delimiter '{tok}'", source_name=tok.source_name, line=tok.line, column=tok.column
            )

        pos += 1
        return parse_atom(tok)

    while pos < len(tokens):
        expressions.append(read_expr())

    return expressions

def parse(source: str, *, max_depth: int | None = 256, source_name: str = "<string>") -> list:
    """Parse source string into a list of s-expressions."""
    tokens = tokenize(source, source_name=source_name)
    return _parse_tokens(tokens, source_name=source_name, source_length=len(source), max_depth=max_depth)

def read_source(source: str, *, max_depth: int | None = 256, source_name: str = "<string>") -> ReadResult:
    """Classify and parse source for batch and interactive readers."""
    try:
        expressions = parse(source, max_depth=max_depth, source_name=source_name)
        return ReadResult(ReadState.COMPLETE, expressions=expressions)
    except LispSyntaxError as exc:
        state = ReadState.INCOMPLETE if exc.incomplete else ReadState.INVALID
        return ReadResult(state, error=exc)
    except InvalidDataError as exc:
        return ReadResult(ReadState.INVALID, error=exc)

def parse_atom(tok: str) -> Any:
    """Parse a single atom token."""
    if tok == "#t":
        return True
    if tok == "#f":
        return False

    if tok == "nil":
        return NIL

    try:
        return int(tok)
    except ValueError:
        pass

    try:
        value = float(tok)
        if not math.isfinite(value):
            raise InvalidDataError(f"non-finite numeric literal: {tok}")
        return value
    except ValueError:
        pass

    if tok.startswith('"') and tok.endswith('"'):
        source = tok[1:-1]
        decoded = []
        index = 0
        escapes = {"n": "\n", "t": "\t", '"': '"', "\\": "\\"}
        while index < len(source):
            if source[index] != "\\":
                decoded.append(source[index])
                index += 1
                continue
            if index + 1 >= len(source):
                decoded.append("\\")
                break
            escaped = source[index + 1]
            if escaped in escapes:
                decoded.append(escapes[escaped])
            else:
                decoded.extend(("\\", escaped))
            index += 2
        return "".join(decoded)

    return Symbol(tok)

def _parse_parameters(params: Any) -> str | Parameters:
    """Parse fixed, variadic, or dotted-rest parameter declarations."""
    if isinstance(params, Symbol):
        return str(params)
    if not isinstance(params, list):
        raise LispError(f"invalid parameters: {params}")

    dots = [i for i, param in enumerate(params) if str(param) == "."]
    if not dots:
        if any(not isinstance(param, Symbol) for param in params):
            raise LispError("parameters must be symbols")
        names = [str(param) for param in params]
        if len(names) != len(set(names)):
            raise LispError("duplicate parameter")
        return Parameters(names)
    if len(dots) != 1 or dots[0] != len(params) - 2:
        raise LispError("'.' must appear once before the final rest parameter")

    dot = dots[0]
    declared = params[:dot] + params[dot + 1 :]
    if any(not isinstance(param, Symbol) for param in declared):
        raise LispError("parameters must be symbols")
    names = [str(param) for param in declared]
    if len(names) != len(set(names)):
        raise LispError("duplicate parameter")
    return Parameters([str(param) for param in params[:dot]], rest=str(params[dot + 1]))

# Value representation

def _validate_value_graph(value, *, max_depth=256, max_nodes=100_000):
    stack = [("enter", value, 0)]
    active = set()
    nodes = 0
    while stack:
        action, current, depth = stack.pop()
        if action == "exit":
            active.remove(current)
            continue
        nodes += 1
        if max_nodes is not None and nodes > max_nodes:
            raise ExecutionLimitExceeded("value_nodes", max_nodes, nodes)
        if max_depth is not None and depth > max_depth:
            raise ExecutionLimitExceeded("value_depth", max_depth, depth)
        if type(current) is float and not math.isfinite(current):
            raise InvalidDataError("non-finite LisPy value")
        if not isinstance(current, (Pair, list, dict)):
            continue
        identity = id(current)
        if identity in active:
            raise InvalidDataError("cyclic LisPy value")
        active.add(identity)
        stack.append(("exit", identity, depth))
        if isinstance(current, Pair):
            children = (current.car, current.cdr)
        elif isinstance(current, list):
            children = current
        else:
            children = tuple(current.items())
        for child in reversed(children):
            if isinstance(current, dict):
                key, item = child
                stack.append(("enter", item, depth + 1))
                stack.append(("enter", key, depth + 1))
            else:
                stack.append(("enter", child, depth + 1))

def _value_repr(val: Any, *, _validated=False) -> str:
    if not _validated:
        _validate_value_graph(val)
    if val is True:
        return "#t"
    if val is False:
        return "#f"
    if val is NIL:
        return "()"
    if isinstance(val, str) and not isinstance(val, Symbol):
        escaped = val.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
        return f'"{escaped}"'
    if isinstance(val, list):
        return "(" + " ".join(_value_repr(x, _validated=True) for x in val) + ")"
    if isinstance(val, dict):
        items = " ".join(
            f"({_value_repr(k, _validated=True)} . {_value_repr(v, _validated=True)})" for k, v in val.items()
        )
        return f"(dict {items})"
    if isinstance(val, Pair):
        return repr(val)
    if isinstance(val, (Lambda, Macro)):
        return repr(val)
    if val is None:
        return "()"
    return str(val)

def display_value(val: Any) -> str:
    """Format a value for display (strings without quotes)."""
    if isinstance(val, str) and not isinstance(val, Symbol):
        return val
    return _value_repr(val)

# Canonical form and program identity (lispy-core@1)

TEACH_PATH = REPO_ROOT / "llms.txt"
MAGIC_LINE = ";; lispy-core@1 — https://kody-w.github.io/lisppy/llms.txt"

def _canonical_form(val: Any) -> str:
    """Print one parsed form so that reformatting never changes the text (nil and () stay distinct)."""
    if val is NIL:
        return "nil"
    if isinstance(val, Symbol):
        return str(val)
    if isinstance(val, bool):
        return "#t" if val else "#f"
    if isinstance(val, str):
        return json.dumps(val, ensure_ascii=False)
    if isinstance(val, (int, float)):
        return repr(val)
    if isinstance(val, list):
        return "(" + " ".join(_canonical_form(x) for x in val) + ")"
    if isinstance(val, Pair):
        items, cur = [], val
        while isinstance(cur, Pair):
            items.append(_canonical_form(cur.car))
            cur = cur.cdr
        tail = "" if cur is NIL else " . " + _canonical_form(cur)
        return "(" + " ".join(items) + tail + ")"
    return _value_repr(val)

def canonical_source(source: str, *, source_name: str = "<string>") -> str:
    """The canonical text of a program: parsed, comments and layout removed, one form per line."""
    forms = parse(source, source_name=source_name)
    return "".join(_canonical_form(f) + "\n" for f in forms)

def program_identity(source: str, *, source_name: str = "<string>") -> str:
    """sha256 of the canonical form, written with its algorithm so a future hash can alias it."""
    text = canonical_source(source, source_name=source_name)
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()

# JSON <-> S-expression conversion

def json_to_lisp(obj: Any) -> Any:
    """Convert a JSON-compatible Python object to a Lisp value."""
    if obj is NIL:
        return NIL
    if obj is None:
        return NIL
    if isinstance(obj, bool):
        return obj
    if isinstance(obj, int):
        return obj
    if isinstance(obj, float):
        if not math.isfinite(obj):
            raise InvalidDataError("non-finite JSON number")
        return obj
    if isinstance(obj, str):
        return obj
    if isinstance(obj, list):
        return [json_to_lisp(item) for item in obj]
    if isinstance(obj, dict):
        return {key: json_to_lisp(value) for key, value in obj.items()}
    raise InvalidDataError(f"unsupported JSON value: {type(obj).__name__}")

def lisp_to_json(val: Any, *, _validated=False) -> Any:
    """Convert a Lisp value back to JSON-compatible Python."""
    if not _validated:
        _validate_value_graph(val)
    if val is NIL or val is None:
        return None
    if type(val) is bool:
        return val
    if type(val) is int:
        return val
    if type(val) is float:
        if not math.isfinite(val):
            raise InvalidDataError("non-finite LisPy number")
        return val
    if isinstance(val, Symbol):
        raise InvalidDataError("symbols are not JSON values")
    if type(val) is str:
        return val
    if isinstance(val, Pair):
        result = []
        current = val
        seen = set()
        while isinstance(current, Pair):
            identity = id(current)
            if identity in seen:
                raise InvalidDataError("cyclic pair is not JSON")
            seen.add(identity)
            result.append(lisp_to_json(current.car, _validated=True))
            current = current.cdr
        if current is not NIL:
            raise InvalidDataError("improper pair is not JSON")
        return result
    if isinstance(val, list):
        return [lisp_to_json(x, _validated=True) for x in val]
    if isinstance(val, dict):
        result = {}
        for key, value in val.items():
            if isinstance(key, Symbol) or type(key) is not str:
                raise InvalidDataError("JSON object keys must be strings")
            result[key] = lisp_to_json(value, _validated=True)
        return result
    raise InvalidDataError(f"unsupported LisPy JSON value: {type(val).__name__}")

def py_to_lisp(val: Any) -> Any:
    """Convert Python value to Lisp-friendly representation."""
    if val is None:
        return NIL
    if isinstance(val, (bool, int, float, str)):
        return val
    if isinstance(val, list):
        return [py_to_lisp(x) for x in val]
    if isinstance(val, dict):
        return {k: py_to_lisp(v) for k, v in val.items()}
    return val

class Core1ValueOps:
    """Centralized observable value behavior for lispy-core@1."""

    @staticmethod
    def truthy(value):
        return value is not False and value is not NIL

    def equal(self, left, right, seen=None):
        pending = [(left, right)]
        compared = set(seen or ())
        while pending:
            current_left, current_right = pending.pop()
            if current_left is current_right:
                continue
            if current_left is NIL or current_right is NIL:
                return False
            if type(current_left) is bool or type(current_right) is bool:
                if not (type(current_left) is bool and type(current_right) is bool and current_left == current_right):
                    return False
                continue
            if isinstance(current_left, Symbol) or isinstance(current_right, Symbol):
                if not (
                    isinstance(current_left, Symbol)
                    and isinstance(current_right, Symbol)
                    and str(current_left) == str(current_right)
                ):
                    return False
                continue
            if type(current_left) in (int, float) and type(current_right) in (int, float):
                if current_left != current_right:
                    return False
                continue
            if type(current_left) is str or type(current_right) is str:
                if not (type(current_left) is str and type(current_right) is str and current_left == current_right):
                    return False
                continue
            if type(current_left) is not type(current_right):
                return False
            if isinstance(current_left, (Pair, list, dict)):
                identities = (id(current_left), id(current_right))
                if identities in compared:
                    continue
                compared.add(identities)
            if isinstance(current_left, Pair):
                pending.append((current_left.cdr, current_right.cdr))
                pending.append((current_left.car, current_right.car))
            elif isinstance(current_left, list):
                if len(current_left) != len(current_right):
                    return False
                pending.extend(zip(current_left, current_right))
            elif isinstance(current_left, dict):
                if set(current_left) != set(current_right):
                    return False
                pending.extend((current_left[key], current_right[key]) for key in current_left)
            elif current_left != current_right:
                return False
        return True

    @staticmethod
    def identical(left, right):
        return left is right

    @staticmethod
    def is_null(value):
        return value is NIL or value is None or (isinstance(value, list) and len(value) == 0)

    @staticmethod
    def is_pair(value):
        return isinstance(value, Pair) or (isinstance(value, list) and len(value) > 0)

    @staticmethod
    def is_list(value):
        return isinstance(value, list)

    from_json = staticmethod(json_to_lisp)
    to_json = staticmethod(lisp_to_json)

    @staticmethod
    def to_wire(value):
        return to_wire(value)

CORE1_VALUES = Core1ValueOps()

# Rappterbook bindings

def set_state_dir(path: str | Path) -> None:
    """Set the read-only state root used by rb-* bindings."""
    global STATE_DIR
    STATE_DIR = Path(path).expanduser().resolve()

def _check_collection_graph(context, value):
    stack = [value]
    seen = set()
    while stack:
        current = stack.pop()
        if type(current) is str:
            context.check_collection(len(current))
            continue
        if isinstance(current, list):
            identity = id(current)
            if identity in seen:
                continue
            seen.add(identity)
            context.check_collection(len(current))
            stack.extend(current)
            continue
        if isinstance(current, dict):
            identity = id(current)
            if identity in seen:
                continue
            seen.add(identity)
            context.check_collection(len(current))
            stack.extend(current.keys())
            stack.extend(current.values())
            continue
        if isinstance(current, Pair):
            count = 0
            chain = set()
            while isinstance(current, Pair):
                identity = id(current)
                if identity in chain:
                    raise InvalidDataError("cyclic LisPy value")
                chain.add(identity)
                count += 1
                context.check_collection(count)
                stack.append(current.car)
                current = current.cdr
            if current is not NIL:
                stack.append(current)
    return value

# Evaluator

def _validated_bindings(bindings, form, *, allow_duplicates):
    if not isinstance(bindings, list):
        raise LispError(f"{form} bindings must be a list")
    names = []
    for binding in bindings:
        if not isinstance(binding, list) or len(binding) != 2 or not isinstance(binding[0], Symbol):
            raise LispError(f"invalid {form} binding: {binding}")
        names.append(str(binding[0]))
    if not allow_duplicates and len(names) != len(set(names)):
        raise LispError(f"duplicate {form} binding")
    return bindings

def evaluate(expr: Any, env: Env) -> Any:
    """Evaluate an s-expression in an environment."""
    env.context.consume()

    if isinstance(expr, (int, float, bool)):
        return expr
    if expr is NIL:
        return NIL
    if isinstance(expr, str) and not isinstance(expr, Symbol):
        return expr
    if isinstance(expr, Pair):
        return expr
    if isinstance(expr, dict):
        return expr

    if isinstance(expr, Symbol):
        return env.find(expr)[expr]

    if not isinstance(expr, list):
        return expr

    if len(expr) == 0:
        return NIL

    head = expr[0]

    if isinstance(head, Symbol):
        if head == "quote":
            if len(expr) != 2:
                raise LispError("quote requires exactly 1 argument")
            return expr[1]

        if head in ("quasiquote", "unquote"):
            raise UnsupportedFormError(f"{head} is not supported by lispy-core@1")

        if head == "if":
            if len(expr) not in (3, 4):
                raise LispError("if requires 2 or 3 arguments")
            test = evaluate(expr[1], env)
            if env.value_ops.truthy(test):
                return evaluate(expr[2], env)
            elif len(expr) > 3:
                return evaluate(expr[3], env)
            else:
                return NIL

        if head == "cond":
            clauses = expr[1:]
            for index, clause in enumerate(clauses):
                if not isinstance(clause, list) or len(clause) < 2:
                    raise LispError("invalid cond clause")
                if isinstance(clause[0], Symbol) and clause[0] == "else" and index != len(clauses) - 1:
                    raise LispError("cond else clause must be last")
            for clause in clauses:
                if isinstance(clause[0], Symbol) and clause[0] == "else":
                    return eval_body(clause[1:], env)
                test = evaluate(clause[0], env)
                if env.value_ops.truthy(test):
                    return eval_body(clause[1:], env)
            return NIL

        if head == "define":
            if len(expr) < 3:
                raise LispError("define requires at least 2 arguments")
            target = expr[1]
            if isinstance(target, list):
                # (define (name params...) body...)
                if not target or not isinstance(target[0], Symbol):
                    raise LispError("define function name must be a symbol")
                name = target[0]
                params = _parse_parameters(target[1:])
                body = expr[2:]
                fn = Lambda(params, body, env, name=str(name))
                env.define(str(name), fn)
                return fn
            else:
                if len(expr) != 3:
                    raise LispError("define variable requires exactly 2 arguments")
                if not isinstance(target, Symbol):
                    raise LispError("define variable name must be a symbol")
                name = str(target)
                env.ensure_definable(name)
                val = evaluate(expr[2], env)
                env.define(name, val)
                return val

        if head == "set!":
            if len(expr) != 3:
                raise LispError("set! requires exactly 2 arguments")
            if not isinstance(expr[1], Symbol):
                raise LispError("set! name must be a symbol")
            name = str(expr[1])
            target = env.find(name)
            target.ensure_assignable(name)
            val = evaluate(expr[2], env)
            target.assign(name, val)
            return val

        if head == "lambda":
            if len(expr) < 3:
                raise LispError("lambda requires params and body")
            params = _parse_parameters(expr[1])
            return Lambda(params, expr[2:], env)

        if head == "let":
            if len(expr) < 3:
                raise LispError("let requires bindings and body")
            bindings = _validated_bindings(expr[1], "let", allow_duplicates=False)
            body = expr[2:]
            new_env = Env(outer=env)
            for binding in bindings:
                name = str(binding[0])
                val = evaluate(binding[1], env)
                new_env[name] = val
            return eval_body(body, new_env)

        if head == "let*":
            if len(expr) < 3:
                raise LispError("let* requires bindings and body")
            bindings = _validated_bindings(expr[1], "let*", allow_duplicates=True)
            body = expr[2:]
            new_env = Env(outer=env)
            for binding in bindings:
                name = str(binding[0])
                val = evaluate(binding[1], new_env)
                new_env[name] = val
            return eval_body(body, new_env)

        if head == "begin":
            return eval_body(expr[1:], env)

        if head == "and":
            result: Any = True
            for arg in expr[1:]:
                result = evaluate(arg, env)
                if not env.value_ops.truthy(result):
                    return result
            return result

        if head == "or":
            for arg in expr[1:]:
                result = evaluate(arg, env)
                if env.value_ops.truthy(result):
                    return result
            return False

        if head == "define-macro":
            if len(expr) < 3:
                raise LispError("define-macro requires at least 2 arguments")
            target = expr[1]
            if isinstance(target, list):
                if not target or not isinstance(target[0], Symbol):
                    raise LispError("macro name must be a symbol")
                name = str(target[0])
                params = _parse_parameters(target[1:])
                body = expr[2:]
                mac = Macro(params, body, env, name=name)
                env.define(name, mac)
                return mac
            raise LispError("invalid define-macro syntax")

        # "do" is reserved until its iteration semantics are specified.
        if head == "do":
            raise UnsupportedFormError("do is not supported by lispy-core@1")

    fn = evaluate(head, env)

    if isinstance(fn, Macro):
        args = expr[1:]  # unevaluated
        env.context.consume()
        env.context.enter_call()
        try:
            macro_env = Env(fn.params, args, fn.env)
            expanded = eval_body(fn.body, macro_env)
            return evaluate(expanded, env)
        finally:
            env.context.exit_call()

    args = [evaluate(arg, env) for arg in expr[1:]]

    return _call_fn(fn, args, env.context, call_name=head)

def eval_body(body: list, env: Env) -> Any:
    """Evaluate a sequence of expressions, return the last result."""
    result: Any = NIL
    for expr in body:
        result = evaluate(expr, env)
    return result

# Standard library (built-in functions)

def _denied(name: str):
    def deny(*_args):
        raise CapabilityDenied(f"capability denied: {name}; rerun the CLI with --trusted")

    return deny

def _json_parse(source, *, context=None):
    try:
        value = json_to_lisp(_strict_json_loads(source))
        if context is not None:
            _check_collection_graph(context, value)
        return value
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise InvalidDataError(f"invalid JSON: {exc}")

def _string_to_number(value):
    if type(value) is not str:
        raise LispError("string->number requires text")
    try:
        if re.fullmatch(r"[+-]?(?:0|[1-9][0-9]*)", value):
            return int(value)
        if re.fullmatch(
            r"[+-]?(?:(?:[0-9]+\.[0-9]*|[0-9]*\.[0-9]+)"
            r"(?:[eE][+-]?[0-9]+)?|[0-9]+[eE][+-]?[0-9]+)",
            value,
        ):
            return _finite_result(float(value))
        raise ValueError
    except ValueError:
        raise LispError(f"not a number: {value}")

def _load_stdlib(env: Env, profile: str) -> None:
    resource = "core-stdlib.lisp" if profile == "core" else "stdlib.lisp"
    fallback = CORE_STDLIB_PATH if profile == "core" else STDLIB_PATH
    try:
        source = _resource_text(resource, fallback)
    except OSError as exc:
        raise LispError(f"cannot load standard library {fallback}: {exc}")
    source_name = str(fallback) if fallback.exists() else f"<package:{resource}>"
    for expr in parse(source, source_name=source_name):
        evaluate(expr, env)

def make_global_env(
    *,
    trusted: bool = False,
    load_stdlib: bool = True,
    limits: ExecutionLimits | None = None,
    profile: str = "default",
    output=None,
    state_dir: str | Path | None = None,
) -> Env:
    """Create a core environment with explicit host capabilities."""
    if profile not in ("core", "default"):
        raise LispError(f"unknown runtime profile: {profile}")
    if profile == "core" and trusted:
        raise LispError("the core profile cannot grant trusted host capabilities")
    host_profile = profile == "default"
    context = ExecutionContext(limits)
    if output is None:
        writer = BoundedWriter(sys.stdout.write, context.limits.max_output_bytes)
    elif isinstance(output, (BoundedOutput, BoundedWriter)):
        writer = output
    else:
        target = output if callable(output) else output.write
        writer = BoundedWriter(target, context.limits.max_output_bytes)
    env = Env(context=context, writer=writer)
    state_root = Path(state_dir if state_dir is not None else STATE_DIR).expanduser().resolve()

    # -- Arithmetic --
    env["+"] = _numeric_add
    env["-"] = _numeric_subtract
    env["*"] = _numeric_multiply
    env["/"] = _numeric_divide
    env["//"] = _numeric_floor_divide
    env["%"] = _numeric_modulo
    env["abs"] = lambda value: _finite_result(abs(_real_number(value)))
    env["min"] = lambda *args: _numeric_extreme(min, "min", args)
    env["max"] = lambda *args: _numeric_extreme(max, "max", args)
    env["floor"] = lambda value: math.floor(_real_number(value))
    env["ceil"] = lambda value: math.ceil(_real_number(value))
    env["round"] = lambda value: _finite_result(round(_real_number(value)))
    env["expt"] = lambda a, b: _finite_result(pow(_real_number(a), _real_number(b)))
    env["pow"] = env["expt"]
    env["sqrt"] = lambda value: _finite_result(math.sqrt(_real_number(value)))
    env["sin"] = lambda value: _finite_result(math.sin(_real_number(value)))
    env["cos"] = lambda value: _finite_result(math.cos(_real_number(value)))
    env["mod"] = _numeric_modulo
    env["modulo"] = env["mod"]
    env["remainder"] = env["mod"]

    # -- Comparison --
    env["="] = env.value_ops.equal
    env["equal?"] = env.value_ops.equal
    env["eq?"] = env.value_ops.identical
    env["<"] = lambda a, b: _numeric_compare(a, b, lambda x, y: x < y)
    env[">"] = lambda a, b: _numeric_compare(a, b, lambda x, y: x > y)
    env["<="] = lambda a, b: _numeric_compare(a, b, lambda x, y: x <= y)
    env[">="] = lambda a, b: _numeric_compare(a, b, lambda x, y: x >= y)
    env["!="] = lambda a, b: not env.value_ops.equal(a, b)

    # -- Logic --
    env["not"] = lambda x: not env.value_ops.truthy(x)

    # -- Type predicates --
    env["null?"] = env.value_ops.is_null
    env["pair?"] = env.value_ops.is_pair
    env["list?"] = env.value_ops.is_list
    env["number?"] = lambda x: isinstance(x, (int, float)) and not isinstance(x, bool)
    env["string?"] = lambda x: isinstance(x, str) and not isinstance(x, Symbol)
    env["symbol?"] = lambda x: isinstance(x, Symbol)
    env["boolean?"] = lambda x: isinstance(x, bool)
    env["procedure?"] = lambda x: callable(x) or isinstance(x, Lambda)
    env["dict?"] = lambda x: isinstance(x, dict) and not isinstance(x, Env)
    env["integer?"] = lambda x: isinstance(x, int) and not isinstance(x, bool)
    env["zero?"] = lambda x: _real_number(x) == 0
    env["positive?"] = lambda x: _real_number(x) > 0
    env["negative?"] = lambda x: _real_number(x) < 0

    # -- List operations --
    env["cons"] = _cons
    env["car"] = lambda x: _car(x)
    env["cdr"] = lambda x: _cdr(x)
    env["list"] = lambda *args: _list_fn(env.context, *args)
    env["length"] = lambda value: _length_fn(env.context, value)
    env["append"] = lambda *lists: _append(*lists, context=env.context)
    env["reverse"] = lambda value: _reverse_fn(env.context, value)
    env["nth"] = lambda values, index: _nth_fn(env.context, values, index)
    env["take"] = lambda values, count: _slice_fn(env.context, values, None, count)
    env["drop"] = lambda values, count: _slice_fn(env.context, values, count, None)
    env["range"] = lambda *args: _range_fn(env.context, *args)
    env["zip"] = lambda *lists: _zip_fn(env.context, *lists)
    env["flatten"] = lambda values: _flatten(values, context=env.context)
    env["sort"] = lambda values, *key_fn: _sort_fn(values, *key_fn, context=env.context)
    env["first"] = lambda x: _car(x)
    env["rest"] = lambda x: _cdr(x)
    env["last"] = lambda values: _last_fn(env.context, values)
    env["empty?"] = lambda x: x is NIL or x is None or (isinstance(x, (list, dict, str)) and len(x) == 0)

    # -- Higher-order functions --
    env["map"] = lambda fn, *lists: _map_fn(fn, *lists, context=env.context)
    env["filter"] = lambda fn, values: _filter_fn(fn, values, context=env.context)
    env["reduce"] = lambda fn, values, *initial: _reduce_fn(fn, values, *initial, context=env.context)
    env["for-each"] = lambda fn, values: _for_each_fn(fn, values, context=env.context)
    env["apply"] = lambda fn, args: _apply_fn(fn, args, context=env.context)
    env["compose"] = lambda f, g: lambda *args: _call_fn(f, [_call_fn(g, list(args))])

    # -- String operations --
    env["string-append"] = lambda *args: _bounded_string(env.context, "".join(str(a) for a in args))
    env["concat"] = env["string-append"]
    env["string-length"] = lambda s: len(s)
    env["substring"] = lambda s, start, *end: _bounded_string(env.context, s[start : end[0]] if end else s[start:])
    env["string-upcase"] = lambda s: _bounded_string(env.context, s.upper())
    env["string-downcase"] = lambda s: _bounded_string(env.context, s.lower())
    env["string-contains?"] = lambda s, sub: sub in s
    env["string-split"] = lambda s, *delim: _string_split(env.context, s, *delim)
    env["string-join"] = lambda values, *separator: _string_join(env.context, values, *separator)
    env["string-trim"] = lambda s: _bounded_string(env.context, s.strip())
    env["string-replace"] = lambda s, old, new: _bounded_string(env.context, s.replace(old, new))
    env["string-ref"] = lambda s, i: s[i]

    # -- Type conversion --
    env["number->string"] = lambda n: str(n)
    env["string->number"] = _string_to_number
    env["string"] = lambda value: str(value)
    env["number"] = _string_to_number
    env["symbol->string"] = lambda s: str(s)
    env["string->symbol"] = lambda s: Symbol(s)
    env["->string"] = lambda x: str(x) if not isinstance(x, str) else x
    env["->number"] = _string_to_number

    # -- Dict operations --
    env["get"] = _get_fn
    env["keys"] = lambda value: _dict_items(env.context, value, keys=True)
    env["values"] = lambda value: _dict_items(env.context, value, keys=False)
    env["has-key?"] = _has_key
    env["dict-set"] = lambda d, k, v: _dict_set(env.context, d, k, v)
    env["dict-merge"] = lambda *dicts: _merge_dicts(*dicts, context=env.context)
    env["dict-map"] = lambda fn, value: _dict_map_fn(fn, value, context=env.context)
    env["dict-filter"] = lambda fn, value: _dict_filter_fn(fn, value, context=env.context)
    env["make-dict"] = lambda *pairs: _make_dict(env.context, *pairs)

    # -- I/O --
    env["display"] = lambda *args: _display(env.writer, *args)
    env["newline"] = lambda: _newline(env.writer)
    env["print"] = lambda x: _print_val(env.writer, x)
    env["println"] = lambda x: _println_val(env.writer, x)
    env["log"] = lambda *args: _println_val(env.writer, " ".join(display_value(x) for x in args))

    if host_profile:
        if trusted:
            env["read-file"] = _hosted_binding("_read_file")
            env["write-file"] = _hosted_binding("_write_file")
            env["file-exists?"] = lambda path: Path(path).exists()
        else:
            env["read-file"] = _denied("filesystem.read")
            env["write-file"] = _denied("filesystem.write")
            env["file-exists?"] = _denied("filesystem.read")

    # -- JSON --
    env["json-parse"] = lambda source: _json_parse(source, context=env.context)
    env["json->sexp"] = env["json-parse"]
    env["json-dump"] = lambda val: _bounded_string(
        env.context, json.dumps(lisp_to_json(val), indent=2, allow_nan=False)
    )
    env["sexp->json"] = env["json-dump"]

    # -- Rappterbook bindings --
    if host_profile:
        rb_state = _hosted_binding("rb_state")
        rb_agent = _hosted_binding("rb_agent")
        rb_soul = _hosted_binding("rb_soul")
        rb_channels = _hosted_binding("rb_channels")
        rb_trending = _hosted_binding("rb_trending")
        env["rb-state"] = lambda filename: rb_state(filename, state_dir=state_root)
        env["rb-agent"] = lambda agent_id: rb_agent(agent_id, state_dir=state_root)
        env["rb-soul"] = lambda agent_id: rb_soul(agent_id, state_dir=state_root)
        env["rb-channels"] = lambda: rb_channels(state_dir=state_root)
        env["rb-trending"] = lambda: rb_trending(state_dir=state_root)
        env["rb-post"] = _hosted_binding("rb_post")
        env["rb-comment"] = _hosted_binding("rb_comment")
        env["rb-react"] = _hosted_binding("rb_react")
        env["rb-run"] = _hosted_binding("rb_run") if trusted else _denied("process.python")

    # -- Special values --
    env["#t"] = True
    env["#f"] = False
    env["true"] = True
    env["false"] = False
    env["nil"] = NIL
    env["pi"] = math.pi
    env["e"] = math.e

    # -- Error handling --
    env["error"] = lambda msg: _raise_error(msg)

    capabilities = ["lispy-core@1", "console"]
    if host_profile:
        capabilities.extend(["rappterbook.read", "rappterbook.plan"])
    if trusted:
        capabilities.extend(["filesystem.read", "filesystem.write", "process.python"])
    env["capabilities"] = lambda: list(capabilities)
    env["runtime-info"] = lambda: {
        "name": "LisPy",
        "version": VERSION,
        "profile": LANGUAGE_PROFILE,
        "profiles": (
            [LANGUAGE_PROFILE] if not host_profile else [LANGUAGE_PROFILE, "rappterbook.read", "rappterbook.plan"]
        ),
        "trusted": trusted,
        "stdlib_loaded": load_stdlib,
        "capabilities": list(capabilities),
        "limits": env.context.limits.as_dict(),
    }
    env["builtin-manifest"] = lambda: sorted(str(name) for name in env)
    env["execution-usage"] = lambda: {
        "steps": env.context.steps,
        "call_depth": env.context.call_depth,
        "peak_call_depth": env.context.peak_call_depth,
    }

    if load_stdlib:
        user_context = env.context
        env.context = ExecutionContext(ExecutionLimits.unlimited())
        try:
            _load_stdlib(env, profile)
        finally:
            env.context = user_context

    return env

# Helper functions for built-ins

def _product(args):
    result = 1
    for a in args:
        result *= a
    return result

def _real_number(value):
    if type(value) not in (int, float):
        raise LispError("numeric operation requires real numbers")
    if type(value) is float and not math.isfinite(value):
        raise LispError("numeric operation requires finite numbers")
    return value

def _real_numbers(values):
    return [_real_number(value) for value in values]

def _numeric_add(*args):
    return _finite_result(sum(_real_numbers(args)))

def _numeric_subtract(first, *rest):
    first = _real_number(first)
    values = _real_numbers(rest)
    return _finite_result(first - sum(values) if values else -first)

def _numeric_multiply(*args):
    return _finite_result(_product(_real_numbers(args)))

def _numeric_divide(first, second):
    first = _real_number(first)
    second = _real_number(second)
    return _finite_result(first / second if second != 0 else _div_error())

def _numeric_floor_divide(first, second):
    first = _real_number(first)
    second = _real_number(second)
    return _finite_result(first // second if second != 0 else _div_error())

def _numeric_modulo(first, second):
    first = _real_number(first)
    second = _real_number(second)
    return _finite_result(first % second if second != 0 else _div_error())

def _numeric_extreme(function, name, values):
    values = _real_numbers(values)
    if not values:
        raise LispError(f"{name} requires at least one argument")
    return _finite_result(function(values))

def _numeric_compare(first, second, comparator):
    return comparator(_real_number(first), _real_number(second))

def _finite_result(value):
    if type(value) not in (int, float):
        raise LispError("numeric operation produced a non-real result")
    if type(value) is float and not math.isfinite(value):
        raise LispError("numeric operation produced non-finite result")
    return value

def _div_error():
    raise LispError("division by zero")

def _raise_error(msg):
    raise LispError(str(msg))

def _is_truthy(value):
    return CORE1_VALUES.truthy(value)

def _cons(a, b):
    """(cons a b). A JSON-born host list is a proper list everywhere else in LisPy (car, cdr, length,
    append), so consing onto one yields a proper list too, never an improper pair (0.25.0)."""
    if isinstance(b, list):
        return [a, *b]
    return Pair(a, b)

def _car(x):
    if isinstance(x, Pair):
        return x.car
    if isinstance(x, list) and len(x) > 0:
        return x[0]
    raise LispError(f"car: not a pair: {_value_repr(x)}")

def _cdr(x):
    if isinstance(x, Pair):
        return x.cdr
    if isinstance(x, list):
        return x[1:] if len(x) > 1 else NIL if len(x) <= 1 else []
    raise LispError(f"cdr: not a pair: {_value_repr(x)}")

def _proper_list_view(value, *, context=None):
    if value is NIL:
        return []
    if isinstance(value, list):
        if context is not None:
            context.check_collection(len(value))
        return value
    if not isinstance(value, Pair):
        raise LispError(f"expected proper list, got {_value_repr(value)}")
    result = []
    seen = set()
    current = value
    while isinstance(current, Pair):
        identity = id(current)
        if identity in seen:
            raise LispError("cyclic pair is not a proper list")
        seen.add(identity)
        result.append(current.car)
        if context is not None:
            context.check_collection(len(result))
            context.consume()
        current = current.cdr
    if current is not NIL:
        raise LispError("improper pair is not a proper list")
    return result

def _length_fn(context, value):
    if value is NIL or isinstance(value, (list, Pair)):
        return len(_proper_list_view(value, context=context))
    if isinstance(value, (str, dict)):
        context.check_collection(len(value))
        return len(value)
    raise LispError(f"length: unsupported value {_value_repr(value)}")

def _append(*lists, context=None):
    normalized = [_proper_list_view(value, context=context) for value in lists]
    size = sum(len(value) for value in normalized)
    if context is not None:
        context.check_collection(size)
        context.consume(size)
    result = []
    for values in normalized:
        result.extend(values)
    return result

def _range_fn(context, *args):
    values = range(*args)
    context.check_collection(len(values))
    context.consume(len(values))
    return list(values)

def _list_fn(context, *args):
    context.check_collection(len(args))
    return list(args)

def _zip_fn(context, *lists):
    normalized = [_proper_list_view(value, context=context) for value in lists]
    size = min((len(value) for value in normalized), default=0)
    context.check_collection(size)
    context.check_collection(size * len(normalized))
    context.consume(size * max(1, len(normalized)))
    return [list(values) for values in zip(*normalized)]

def _flatten(lst, *, context=None):
    values = _proper_list_view(lst, context=context)
    result = []
    stack = [("enter", values, 0)]
    active = set()
    max_depth = context.limits.max_reader_depth if context is not None else 256
    while stack:
        action, item, depth = stack.pop()
        if action == "exit":
            active.remove(item)
            continue
        if item is NIL or isinstance(item, (list, Pair)):
            nested = _proper_list_view(item, context=context)
            identity = id(item)
            if identity in active:
                raise LispError("cyclic list cannot be flattened")
            if max_depth is not None and depth > max_depth:
                raise ExecutionLimitExceeded("collection_depth", max_depth, depth)
            active.add(identity)
            stack.append(("exit", identity, depth))
            for value in reversed(nested):
                stack.append(("enter", value, depth + 1))
            continue
        result.append(item)
        if context is not None:
            context.check_collection(len(result))
            context.consume()
    return result

def _reverse_fn(context, value):
    values = _proper_list_view(value, context=context)
    context.consume(len(values))
    return list(reversed(values))

def _slice_fn(context, values, start, end):
    values = _proper_list_view(values, context=context)
    for bound in (start, end):
        if bound is not None and (isinstance(bound, bool) or not isinstance(bound, int) or bound < 0):
            raise LispError("take/drop count must be a non-negative integer")
    result = values[start:end]
    context.check_collection(len(result))
    context.consume(len(result))
    return result

def _nth_fn(context, values, index):
    values = _proper_list_view(values, context=context)
    if not isinstance(index, int) or isinstance(index, bool):
        raise LispError("nth index must be an integer")
    if index < 0:
        index += len(values)
    return values[index] if 0 <= index < len(values) else NIL

def _last_fn(context, values):
    values = _proper_list_view(values, context=context)
    return values[-1] if values else NIL

def _string_split(context, value, *delimiter):
    result = value.split(delimiter[0] if delimiter else None)
    context.check_collection(len(result))
    context.consume(len(result))
    return result

def _bounded_string(context, value):
    context.check_collection(len(value))
    return value

def _string_join(context, values, *separator):
    items = _proper_list_view(values, context=context)
    return _bounded_string(context, (separator[0] if separator else " ").join(str(item) for item in items))

def _make_dict(context, *pairs):
    if len(pairs) % 2:
        raise LispError("make-dict requires key/value pairs")
    size = len(pairs) // 2
    context.check_collection(size)
    context.consume(size)
    result = {}
    for key, value in zip(pairs[::2], pairs[1::2]):
        key = _string_key(key)
        if key in result:
            raise LispError(f"duplicate map key: {key}")
        result[key] = value
    return result

def _sort_fn(lst, *key_fn, context=None):
    lst = _proper_list_view(lst, context=context)
    if context is not None:
        context.check_collection(len(lst))
        context.consume(len(lst))
    if len(key_fn) > 1:
        raise LispError("sort accepts at most one comparator")
    if key_fn:
        fn = key_fn[0]
        # The comparator returns true if a should come before b
        import functools

        def cmp(a, b):
            if _is_truthy(_call_fn(fn, [a, b], context)):
                return -1
            if _is_truthy(_call_fn(fn, [b, a], context)):
                return 1
            return 0

        return sorted(lst, key=functools.cmp_to_key(cmp))
    if all(type(item) in (int, float) for item in lst):
        return sorted(lst)
    if all(type(item) is str for item in lst):
        return sorted(lst)
    raise LispError("sort without comparator requires homogeneous numbers or strings")

def _map_fn(fn, *lists, context=None):
    if not lists:
        raise LispError("map requires a function and at least one list")
    normalized = [_proper_list_view(value, context=context) for value in lists]
    if len(normalized) == 1:
        return [_call_fn(fn, [item], context) for item in normalized[0]]
    min_len = min(len(value) for value in normalized)
    if context is not None:
        context.check_collection(min_len)
    return [_call_fn(fn, [value[i] for value in normalized], context) for i in range(min_len)]

def _filter_fn(fn, lst, *, context=None):
    lst = _proper_list_view(lst, context=context)
    if context is not None:
        context.check_collection(len(lst))
    return [x for x in lst if _is_truthy(_call_fn(fn, [x], context))]

def _reduce_fn(fn, lst, *init, context=None):
    lst = _proper_list_view(lst, context=context)
    if len(init) > 1:
        raise LispError("reduce accepts at most one initial value")
    if init:
        acc = init[0]
        items = lst
    else:
        if len(lst) == 0:
            raise LispError("reduce on empty list with no initial value")
        acc = lst[0]
        items = lst[1:]
    for item in items:
        acc = _call_fn(fn, [acc, item], context)
    return acc

def _for_each_fn(fn, lst, *, context=None):
    lst = _proper_list_view(lst, context=context)
    for x in lst:
        _call_fn(fn, [x], context)
    return NIL

def _apply_fn(fn, args, *, context=None):
    args = _proper_list_view(args, context=context)
    return _call_fn(fn, args, context)

def _call_fn(fn, args, context=None, call_name=None):
    """Call a function (built-in or Lambda) with args."""
    if context is None and isinstance(fn, Lambda):
        context = fn.env.context
    if context is not None:
        context.consume()

    if callable(fn) and not isinstance(fn, Lambda):
        try:
            return fn(*args)
        except LispError:
            raise
        except (AttributeError, ArithmeticError, IndexError, KeyError, OSError, TypeError, ValueError) as exc:
            name = call_name if call_name is not None else fn
            raise LispError(f"call error ({_value_repr(name)}): {exc}")
    if isinstance(fn, Lambda):
        if context is not None:
            context.enter_call()
        try:
            call_env = Env(fn.params, args, fn.env)
            return eval_body(fn.body, call_env)
        finally:
            if context is not None:
                context.exit_call()
    raise LispError(f"not callable: {_value_repr(fn)}")

def _get_fn(obj, key, *default):
    """Get a value from a dict or list."""
    dflt = default[0] if default else NIL
    if isinstance(obj, dict):
        _string_key(key)
        return obj.get(key, dflt)
    if isinstance(obj, list):
        if type(key) is not int:
            raise LispError("list index must be an integer")
        idx = key
        return obj[idx] if 0 <= idx < len(obj) else dflt
    return dflt

def _string_key(key):
    if type(key) is not str:
        raise LispError("map keys must be strings")
    return key

def _validated_dict(value):
    if not isinstance(value, dict):
        raise LispError("expected map")
    for key in value:
        _string_key(key)
    return value

def _has_key(value, key):
    return _string_key(key) in _validated_dict(value)

def _dict_set(context, value, key, item):
    value = _validated_dict(value)
    key = _string_key(key)
    size = len(value) + (0 if key in value else 1)
    context.check_collection(size)
    return {**value, key: item}

def _merge_dicts(*dicts, context=None):
    result = {}
    for d in dicts:
        result.update(_validated_dict(d))
        if context is not None:
            context.check_collection(len(result))
    return result

def _dict_items(context, value, *, keys):
    value = _validated_dict(value)
    context.check_collection(len(value))
    context.consume(len(value))
    return list(value.keys() if keys else value.values())

def _dict_map_fn(fn, d, *, context=None):
    d = _validated_dict(d)
    if context is not None:
        context.check_collection(len(d))
    return {k: _call_fn(fn, [k, v], context) for k, v in d.items()}

def _dict_filter_fn(fn, d, *, context=None):
    d = _validated_dict(d)
    if context is not None:
        context.check_collection(len(d))
    return {k: v for k, v in d.items() if _is_truthy(_call_fn(fn, [k, v], context))}

def _display(writer, *args):
    for a in args:
        writer(display_value(a))
    return NIL

def _newline(writer):
    writer("\n")
    return NIL

def _print_val(writer, x):
    writer(_value_repr(x))
    return NIL

def _println_val(writer, x):
    writer(display_value(x) + "\n")
    return NIL

# REPL

def repl(env: Env | None = None):
    """Run an interactive REPL."""
    if env is None:
        env = make_global_env()

    print(f"LisPy v{VERSION} ({LANGUAGE_PROFILE})")
    print("Code is data. Data is code. The REPL is the heartbeat.")
    print("Type (help) for available commands, or Ctrl-D to exit.\n")

    env["help"] = lambda: _repl_help()

    buffer = ""
    while True:
        try:
            prompt = "...  " if buffer else "\u03bb> "
            line = input(prompt)
        except EOFError:
            if buffer:
                result = read_source(buffer, max_depth=env.context.limits.max_reader_depth, source_name="<repl>")
                if result.error is not None:
                    print(f"\n; error: {result.error}")
            print("\n; farewell")
            break
        except KeyboardInterrupt:
            print("\n; interrupted")
            buffer = ""
            continue

        try:
            buffer += ("\n" if buffer else "") + line
            env.context.check_source(buffer)
            read_result = read_source(buffer, max_depth=env.context.limits.max_reader_depth, source_name="<repl>")
            if read_result.state == ReadState.INCOMPLETE:
                continue
            if read_result.state == ReadState.INVALID:
                raise read_result.error
            if not read_result.expressions:
                buffer = ""
                continue
            env.context.reset()
            if hasattr(env.writer, "reset"):
                env.writer.reset()
            for expr in read_result.expressions:
                result = evaluate(expr, env)
                if result is not NIL and result is not None:
                    env.writer(f"=> {_value_repr(result)}\n")
        except LispError as e:
            print(f"; error: {e}")
        except KeyboardInterrupt:
            print("\n; interrupted")
            env.context.reset()
            if hasattr(env.writer, "reset"):
                env.writer.reset()
            buffer = ""
            continue

        buffer = ""

def _repl_help():
    """Print REPL help."""
    help_text = """
; LisPy built-in commands:
;
; Rappterbook:
;   (rb-state "file.json")     Read a state file
;   (rb-agent "agent-id")      Get agent profile
;   (rb-soul "agent-id")       Read agent soul file
;   (rb-channels)              List all channels
;   (rb-trending)              Get trending posts
;   (rb-post ch title body)    Create a typed dry-run post intent
;   (rb-comment num body)      Create a typed dry-run comment intent
;   (rb-react id reaction)     Create a typed dry-run reaction intent
;   (rb-run "python code")     Execute Python (--trusted only)
;   (capabilities)             List enabled host capabilities
;   (runtime-info)             Describe this runtime and profile
;   (builtin-manifest)         List all bound names
;
; Core Lisp:
;   define, lambda, if, cond, let, let*, begin, quote
;   car, cdr, cons, list, map, filter, reduce
;   +, -, *, /, =, <, >, and, or, not
;   display, newline, print, println
;
; Data:
;   get, keys, values, has-key?, make-dict
;   json-parse, json-dump
;   string-append, string-split, string-join
;   number->string, string->number
;
; Type predicates:
;   null?, pair?, list?, number?, string?, symbol?, dict?
"""
    print(help_text)
    return NIL

# Script runner

def run_file(path: str, env: Env | None = None):
    """Execute a Lisp file."""
    if env is None:
        env = make_global_env()

    try:
        with Path(path).open("rb") as source_file:
            limit = env.context.limits.max_source_bytes
            raw = source_file.read() if limit is None else source_file.read(limit + 1)
    except OSError as exc:
        raise LispError(f"cannot read script {path}: {exc}")
    if limit is not None and len(raw) > limit:
        raise ExecutionLimitExceeded("source_bytes", limit, len(raw))
    try:
        source = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise LispSyntaxError(f"script is not valid UTF-8: {exc}", source_name=str(path))
    return run_string(source, env, source_name=str(path))

def run_string(source: str, env: Env | None = None, *, source_name: str = "<string>"):
    """Execute a Lisp string."""
    if env is None:
        env = make_global_env()

    env.context.reset()
    if hasattr(env.writer, "reset"):
        env.writer.reset()
    env.context.check_source(source)
    exprs = parse(source, max_depth=env.context.limits.max_reader_depth, source_name=source_name)
    result = NIL
    for expr in exprs:
        result = evaluate(expr, env)
    _validate_value_graph(result, max_depth=env.context.limits.max_reader_depth, max_nodes=100_000)
    return _check_collection_graph(env.context, result)

class ExecutionResult:
    """Result returned by the isolated embedding API."""

    def __init__(self, *, ok, value, output, error, usage):
        self.ok = ok
        self.value = value
        self.output = output
        self.error = error
        self.usage = usage

    def as_dict(self):
        value_wire = to_wire(self.value) if self.ok else None
        try:
            from lispy_hosted import _receipt_value

            value = _receipt_value(self.value) if self.ok else None
        except (TypeError, ValueError):
            value = None
        return {
            "ok": self.ok,
            "value": value,
            "value_wire": value_wire,
            "output": self.output,
            "error": self.error,
            "usage": dict(self.usage),
        }

    def as_wire_dict(self):
        return {
            "api": "lispy.execution-result/v1",
            "ok": self.ok,
            "value": to_wire(self.value) if self.ok else None,
            "output": self.output,
            "error": self.error,
            "usage": dict(self.usage),
        }

class LispyVM:
    """Reusable one-shot VM with isolated state and captured output."""

    def __init__(
        self,
        *,
        state_root: str | Path | None = None,
        trusted: bool = False,
        load_stdlib: bool = True,
        limits: ExecutionLimits | None = None,
        profile: str = "default",
    ):
        self.state_root = Path(state_root if state_root is not None else STATE_DIR).expanduser().resolve()
        self.trusted = trusted
        self.load_stdlib = load_stdlib
        self.profile = profile
        configured = limits or ExecutionLimits()
        self.limits = ExecutionLimits(**configured.as_dict())

    def execute(self, source: str) -> ExecutionResult:
        output = BoundedOutput(self.limits.max_output_bytes)
        env = make_global_env(
            trusted=self.trusted,
            load_stdlib=self.load_stdlib,
            limits=ExecutionLimits(**self.limits.as_dict()),
            output=output,
            state_dir=self.state_root,
            profile=self.profile,
        )
        try:
            value = run_string(source, env, source_name="<vm>")
            _validate_value_graph(value, max_depth=env.context.limits.max_reader_depth, max_nodes=100_000)
            to_wire(value)
            return ExecutionResult(
                ok=True,
                value=value,
                output=output.getvalue(),
                error=None,
                usage={"steps": env.context.steps, "peak_call_depth": env.context.peak_call_depth},
            )
        except LispSyntaxError as exc:
            phase = "parse"
            error = exc
        except LispError as exc:
            phase = "evaluate"
            error = exc

        details = {"phase": phase, "type": type(error).__name__, "category": error.category, "message": str(error)}
        if isinstance(error, ExecutionLimitExceeded):
            details.update({"resource": error.resource, "limit": error.limit, "used": error.used})
        if isinstance(error, LispSyntaxError):
            details["location"] = {"source": error.source_name, "line": error.line, "column": error.column}
        return ExecutionResult(
            ok=False,
            value=NIL,
            output=output.getvalue(),
            error=details,
            usage={"steps": env.context.steps, "peak_call_depth": env.context.peak_call_depth},
        )

def to_wire(value: Any, *, _seen=None) -> dict:
    """Encode a LisPy value using the portable lispy-value@1 wire."""
    if _seen is None:
        _validate_value_graph(value)
        _seen = set()
    if value is NIL:
        return {"tag": "nil"}
    if type(value) is bool:
        return {"tag": "boolean", "value": value}
    if type(value) is int:
        return {"tag": "integer", "value": str(value)}
    if type(value) is float:
        if not math.isfinite(value):
            raise WireEncodingError("non-finite float is not portable")
        return {"tag": "float64", "bits": struct.pack(">d", value).hex()}
    if isinstance(value, Symbol):
        return {"tag": "symbol", "value": str(value)}
    if type(value) is str:
        return {"tag": "string", "value": value}

    if isinstance(value, (Pair, list, dict)):
        identity = id(value)
        if identity in _seen:
            raise WireEncodingError("cyclic value is not portable")
        _seen.add(identity)
        try:
            if isinstance(value, Pair):
                return {"tag": "pair", "car": to_wire(value.car, _seen=_seen), "cdr": to_wire(value.cdr, _seen=_seen)}
            if type(value) is list:
                return {"tag": "list", "items": [to_wire(item, _seen=_seen) for item in value]}
            entries = [[to_wire(key, _seen=_seen), to_wire(item, _seen=_seen)] for key, item in value.items()]
            entries.sort(
                key=lambda entry: json.dumps(
                    entry[0], ensure_ascii=False, sort_keys=True, separators=(",", ":")
                ).encode("utf-8")
            )
            encoded_keys = [
                json.dumps(entry[0], ensure_ascii=False, sort_keys=True, separators=(",", ":")) for entry in entries
            ]
            if len(encoded_keys) != len(set(encoded_keys)):
                raise WireEncodingError("map contains duplicate portable keys")
            return {"tag": "map", "entries": entries}
        finally:
            _seen.remove(identity)

    raise WireEncodingError(f"unsupported portable value: {type(value).__name__}")

def _check_json_complexity(source):
    depth = 0
    in_string = False
    escaped = False
    for char in source:
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char in "[{":
            depth += 1
            if depth > WORKER_JSON_MAX_DEPTH:
                raise ValueError("JSON nesting exceeds limit")
        elif char in "]}":
            depth -= 1
            if depth < 0:
                break

def _check_json_nodes(value):
    stack = [value]
    count = 0
    while stack:
        current = stack.pop()
        count += 1
        if count > WORKER_JSON_MAX_NODES:
            raise ValueError("JSON node count exceeds limit")
        if isinstance(current, list):
            stack.extend(current)
        elif isinstance(current, dict):
            stack.extend(current.values())

def _strict_json_loads(source: str):
    def object_pairs(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"duplicate JSON key: {key}")
            result[key] = value
        return result

    def reject_constant(value):
        raise ValueError(f"non-finite JSON number: {value}")

    def parse_integer(value):
        if len(value) > 128:
            raise ValueError("JSON integer token exceeds limit")
        return int(value)

    def parse_float(value):
        if len(value) > 128:
            raise ValueError("JSON float token exceeds limit")
        parsed = float(value)
        if not math.isfinite(parsed):
            raise ValueError("non-finite JSON float")
        return parsed

    _check_json_complexity(source)
    try:
        value = json.loads(
            source,
            object_pairs_hook=object_pairs,
            parse_constant=reject_constant,
            parse_int=parse_integer,
            parse_float=parse_float,
        )
    except RecursionError:
        raise ValueError("JSON nesting exceeds decoder limit")
    _check_json_nodes(value)
    return value

# Entry point

def _print_result(result: Any, writer=None) -> None:
    if result is not NIL and result is not None:
        text = _value_repr(result) + "\n"
        (writer or sys.stdout.write)(text)

def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return parsed

EXECUTION_LIMIT_DEFAULTS = {
    "max_steps": 100_000,
    "max_call_depth": 128,
    "max_reader_depth": 256,
    "max_source_bytes": 1_048_576,
    "max_collection_items": 100_000,
    "max_output_bytes": 1_048_576,
}

class LispyArgumentParser(argparse.ArgumentParser):
    json_errors = False

    def error(self, message):
        if self.json_errors:
            print(
                json.dumps(
                    {
                        "api": "lispy.error/v1",
                        "ok": False,
                        "error": {"category": "usage", "code": "invalid_arguments", "message": message},
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                )
            )
            raise SystemExit(2)
        super().error(message)

def _argument_parser() -> argparse.ArgumentParser:
    parser = LispyArgumentParser(
        prog="lispy", description="A zero-dependency Lisp interpreter for agent orchestration.", allow_abbrev=False
    )
    parser.add_argument("script", nargs="?", help="Lisp source file to execute")
    parser.add_argument("-e", "--eval", dest="expression", help="evaluate source")
    parser.add_argument("--print-result", action="store_true", help="print the final value returned by a script")
    parser.add_argument(
        "--trusted", action="store_true", help="enable host filesystem and Python process capabilities"
    )
    parser.add_argument("--no-stdlib", action="store_true", help="start without loading stdlib.lisp")
    parser.add_argument("--state-dir", metavar="PATH", help="read-only root for rb-state and related bindings")
    parser.add_argument(
        "--identity", action="store_true", help="print the program identity (sha256 of its canonical form) and exit"
    )
    parser.add_argument("--format", action="store_true", help="print the program in canonical form and exit")
    parser.add_argument(
        "--teach", action="store_true", help="print llms.txt, the one page that teaches the whole language, and exit"
    )
    for name, description in (
        ("max_steps", "maximum evaluator steps per run"),
        ("max_call_depth", "maximum LisPy call depth per run"),
        ("max_reader_depth", "maximum nested reader depth"),
        ("max_source_bytes", "maximum UTF-8 source size"),
        ("max_collection_items", "maximum items in bounded collection operations"),
        ("max_output_bytes", "maximum captured output bytes for hosted runs"),
    ):
        parser.add_argument(
            "--" + name.replace("_", "-"), type=_positive_int, default=None,
            help=f"{description} (default: {EXECUTION_LIMIT_DEFAULTS[name]})",
        )
    parser.add_argument(
        "--unlimited", action="store_true", help="disable in-process execution limits (does not grant capabilities)"
    )
    parser.add_argument("--jsonl", action="store_true", help="run the supervised lispy.worker/v1 JSONL host adapter")
    parser.add_argument(
        "--doctor",
        nargs="?",
        const="replay@1",
        choices="replay@1 installed@1 effects@1 release@1 inventory@1 replay@2 effects@2 release@2".split(),
        help="run a named deterministic diagnostic profile",
    )
    parser.add_argument("--doctor-mode", choices=("source", "installed"), help="resource mode for doctor v3 profiles")
    parser.add_argument(
        "--expect-inventory", metavar="SHA256", help="require an external doctor inventory SHA-256 pin"
    )
    parser.add_argument("--export-replay", metavar="PATH", help="write a lispy.replay-bundle/v1 during --doctor")
    parser.add_argument("--replay", metavar="PATH", help="verify and reproduce a replay bundle")
    parser.add_argument("--expect-bundle", metavar="SHA256", help="require an external replay-bundle SHA-256 pin")
    parser.add_argument("--expect-artifact", metavar="SHA256", help="require an external worker-artifact SHA-256 pin")
    parser.add_argument("--json", action="store_true", dest="json_output", help="emit --doctor output as JSON")
    parser.add_argument("--worker-once", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--version", action="version", version=f"LisPy {VERSION} ({LANGUAGE_PROFILE})")
    return parser

EXECUTION_OPTION_FLAGS = {
    name: "--" + name.replace("_", "-")
    for name in ("print_result", "trusted", "no_stdlib", "state_dir", *EXECUTION_LIMIT_DEFAULTS, "unlimited")
}

def _require_nonempty_option(parser, value, flag):
    if value is not None and value == "":
        parser.error(f"{flag} requires a non-empty value")

def _dispatch_special_mode(args, parser):
    if args.teach:
        sys.stdout.write(_resource_text("llms.txt", TEACH_PATH))
        return 0
    if args.identity or args.format:
        if args.expression is not None:
            source, name = args.expression, "<eval>"
        elif args.script:
            source, name = Path(args.script).read_text(encoding="utf-8"), args.script
        else:
            parser.error("--identity/--format need a script or --eval")
        if args.identity:
            print(program_identity(source, source_name=name))
        else:
            sys.stdout.write(canonical_source(source, source_name=name))
        return 0
    if (
        args.worker_once or args.replay is not None or args.doctor is not None
        or args.export_replay is not None or args.expect_bundle is not None
        or args.expect_artifact is not None or args.doctor_mode is not None
        or args.expect_inventory is not None or args.json_output or args.jsonl
    ):
        from lispy_hosted import _dispatch_hosted_mode

        return _dispatch_hosted_mode(args, parser)
    return None

def _render_cli_error(args, error):
    if args.json_output:
        print(
            json.dumps(
                {
                    "api": "lispy.error/v1",
                    "ok": False,
                    "error": {
                        "category": getattr(error, "category", "host"),
                        "code": _cli_error_code(error),
                        "type": type(error).__name__,
                        "message": str(error),
                    },
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )
    else:
        print(f"; error: {error}", file=sys.stderr)

def _cli_error_code(error):
    if isinstance(error, ExecutionLimitExceeded):
        return "resource_limit"
    if isinstance(error, LispSyntaxError):
        return "syntax_error"
    if isinstance(error, InvalidDataError):
        return "invalid_data"
    if isinstance(error, CapabilityDenied):
        return "capability_denied"
    if isinstance(error, OSError):
        return "host_io_error"
    return "evaluation_failed"

def main(argv: list[str] | None = None) -> int:
    parser = _argument_parser()
    arguments = list(sys.argv[1:] if argv is None else argv)
    parser.json_errors = "--json" in arguments
    args = parser.parse_args(arguments)
    try:
        special_result = _dispatch_special_mode(args, parser)
    except (LispError, OSError) as exc:
        _render_cli_error(args, exc)
        return 1
    if special_result is not None:
        return special_result
    if args.script and args.expression is not None:
        parser.error("script and --eval cannot be used together")
    _require_nonempty_option(parser, args.state_dir, "--state-dir")
    explicit_limits = [
        flag
        for name, flag in EXECUTION_OPTION_FLAGS.items()
        if name.startswith("max_") and getattr(args, name) is not None
    ]
    if args.unlimited and explicit_limits:
        parser.error("--unlimited cannot be combined with: " + ", ".join(sorted(explicit_limits)))
    if args.state_dir is not None:
        set_state_dir(args.state_dir)

    try:
        limits = (
            ExecutionLimits.unlimited()
            if args.unlimited
            else ExecutionLimits(
                **{
                    name: (getattr(args, name) if getattr(args, name) is not None else default)
                    for name, default in EXECUTION_LIMIT_DEFAULTS.items()
                }
            )
        )
        env = make_global_env(trusted=args.trusted, load_stdlib=not args.no_stdlib, limits=limits)

        if args.expression is not None:
            _print_result(run_string(args.expression, env, source_name="<command-line>"), env.writer)
            return 0

        if args.script:
            result = run_file(args.script, env)
            if args.print_result:
                _print_result(result, env.writer)
            return 0

        if not sys.stdin.isatty():
            limit = env.context.limits.max_source_bytes
            stream = getattr(sys.stdin, "buffer", None)
            try:
                if stream is None:
                    source = sys.stdin.read()
                    raw = source.encode("utf-8")
                else:
                    raw = stream.read() if limit is None else stream.read(limit + 1)
                    source = raw.decode("utf-8")
            except UnicodeError as exc:
                raise LispSyntaxError(f"stdin is not valid text: {exc}", source_name="<stdin>")
            if limit is not None and len(raw) > limit:
                raise ExecutionLimitExceeded("source_bytes", limit, len(raw))
            if source.strip():
                _print_result(run_string(source, env, source_name="<stdin>"), env.writer)
            return 0

        repl(env)
        return 0
    except LispError as exc:
        _render_cli_error(args, exc)
        return 1

# Hosting remains available through the original module without loading it for language runs.
_HOSTED_NAMES = frozenset(
    """
HostedValidationError _state_path rb_state rb_agent rb_soul rb_channels rb_trending _required_text rb_post
rb_comment rb_react effect_idempotency_key effect_digest rb_run _read_file _write_file _copy_host_value
_receipt_value _hosted_error_receipt run_hosted_governor WORKER_API WORKER_MAX_LINE_BYTES
WORKER_MAX_RESPONSE_BYTES WORKER_MAX_STDERR_BYTES WORKER_TIMEOUT_SECONDS DOCTOR_V3_MAX_REPORT_BYTES
_protocol_error _validate_worker_response_shape _validate_request_fields _worker_limits _validate_mars_governor
_registered_sources registered_source run_registered_governor _artifact_sha256 _worker_manifest
_handle_worker_request _json_line _worker_environment _run_worker_once _pump_worker_pipe _feed_worker_stdin
_kill_worker _supervise_worker _run_jsonl_supervisor SAMPLE_STATE_FILES _sample_state_sha256
_doctor_worker_request _canonical_sha256 _normalize_sha256_pin _verify_worker_response_digests _doctor_request
_doctor_report _print_doctor_report _collect_replay_evidence _doctor_check INSTALLED_REQUIRED_PATHS
BUILD_INPUT_PATHS _verify_distribution_record _verify_record_content _validate_distribution_record_paths
_installed_doctor_profile _effects_doctor_profile _doctor_v2 _source_inventory build_source_sha256
_inventory_payload _v3_component _v3_failed_component DOCTOR_V3_CHECK_CATALOGS _validate_doctor_v3_error
_validate_doctor_v3_sha256 _validate_doctor_v3_report _doctor_v3 _run_doctor _run_replay _reject_present_options
base64 csv importlib signal subprocess tempfile threading time
""".split()
)

__all__ = sorted(name for name in set(globals()) | _HOSTED_NAMES if not name.startswith("_"))

def __getattr__(name):
    if name in _HOSTED_NAMES:
        import lispy_hosted

        return getattr(lispy_hosted, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

def __dir__():
    return sorted(set(globals()) | _HOSTED_NAMES)

def _hosted_binding(name):
    def call(*args, **kwargs):
        return getattr(sys.modules[__name__], name)(*args, **kwargs)

    return call

if __name__ == "__main__":
    raise SystemExit(main())
