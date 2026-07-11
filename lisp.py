#!/usr/bin/env python3
"""
LisPy — A Lisp interpreter with capability-gated Rappterbook bindings.

The Rappterbook platform's state is JSON files mutated frame by frame.
The output of frame N is the input to frame N+1. This is literally a REPL:
Read state -> Eval agents -> Print mutations -> Loop.

Lisp's homoiconicity (code is data, data is code) maps perfectly to this
pattern — the state IS the program.

Usage:
    python3 lisp.py                            # interactive REPL
    python3 lisp.py -e '(+ 1 2 3)'            # evaluate an expression
    python3 lisp.py script.lisp                # run a file
    echo '(rb-trending)' | python3 lisp.py     # pipe mode
"""
from __future__ import annotations

import argparse
import base64
import bisect
import csv
import hashlib
import importlib.metadata
import json
import math
import os
import pkgutil
import re
import signal
import struct
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

VERSION = "0.24.0"
LANGUAGE_PROFILE = "lispy-core@1"
DISTRIBUTION_NAME = "rappterbook-lispy-runtime"
REPO_ROOT = Path(__file__).resolve().parent
STDLIB_PATH = REPO_ROOT / "stdlib.lisp"
CORE_STDLIB_PATH = REPO_ROOT / "core-stdlib.lisp"
MAX_STATE_BYTES = 8_388_608


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
STATE_DIR = Path(
    os.environ.get("STATE_DIR", str(Path.cwd() / "state"))
).expanduser().resolve()


# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------

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
            raise ExecutionLimitExceeded(
                "output_bytes", self.max_bytes, used
            )
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
            raise ExecutionLimitExceeded(
                "output_bytes", self.max_bytes, used
            )
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


# ---------------------------------------------------------------------------
# Environment
# ---------------------------------------------------------------------------

class Env(dict):
    """An environment frame with lexical scoping."""

    def __init__(
        self,
        params=(),
        args=(),
        outer=None,
        *,
        context: ExecutionContext | None = None,
        writer=None,
    ):
        super().__init__()
        self.context = (
            context
            or (outer.context if outer is not None else ExecutionContext())
        )
        self.value_ops = (
            outer.value_ops if outer is not None else CORE1_VALUES
        )
        self.writer = writer or (
            outer.writer if outer is not None else sys.stdout.write
        )
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
                raise LispError(
                    f"expected at least {len(required)} args, got {len(args)}"
                )
            if params.rest is None and len(args) != len(required):
                raise LispError(
                    f"expected {len(required)} args, got {len(args)}"
                )
            self.update(zip(required, args[: len(required)]))
            if params.rest is not None:
                self[params.rest] = py_to_lisp(list(args[len(required) :]))
        else:
            if len(params) != len(args):
                raise LispError(
                    f"expected {len(params)} args, got {len(args)}"
                )
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


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------

class LispError(Exception):
    category = "evaluation"

    pass


class LispSyntaxError(LispError):
    category = "syntax"

    def __init__(
        self,
        message,
        *,
        source_name="<string>",
        line=1,
        column=1,
        opener=None,
        incomplete=False,
    ):
        self.source_name = source_name
        self.line = line
        self.column = column
        self.opener = opener
        self.incomplete = incomplete
        super().__init__(f"{source_name}:{line}:{column}: {message}")


class CapabilityDenied(LispError):
    category = "capability"

    pass


class ExecutionLimitExceeded(LispError):
    category = "resource-limit"

    def __init__(self, resource: str, limit: int, used: int):
        self.resource = resource
        self.limit = limit
        self.used = used
        super().__init__(
            f"execution limit exceeded: {resource} "
            f"(limit {limit}, used {used})"
        )


class HostedValidationError(LispError):
    category = "validation"

    def __init__(self, violations):
        self.violations = list(violations)
        super().__init__("; ".join(str(item) for item in self.violations))


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


# ---------------------------------------------------------------------------
# Tokenizer & Parser
# ---------------------------------------------------------------------------

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

        # skip whitespace
        if c in " \t\n\r":
            i += 1
            continue

        # line comment
        if c == ";":
            while i < len(source) and source[i] != "\n":
                i += 1
            continue

        # special characters
        if c in "()[]'`,":
            tokens.append(token(c, i))
            i += 1
            continue

        # string
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
                    "unterminated string",
                    source_name=source_name,
                    line=line,
                    column=column,
                    incomplete=True,
                )
            tokens.append(token(source[i : j + 1], i))
            i = j + 1
            continue

        # atom (symbol / number)
        j = i
        while j < len(source) and source[j] not in " \t\n\r()[]\";,'`":
            j += 1
        tokens.append(token(source[i:j], i))
        i = j

    return tokens


def _parse_tokens(
    tokens,
    *,
    source_name,
    source_length,
    max_depth,
):
    expressions = []
    pos = 0

    def read_expr(depth=0):
        nonlocal pos
        if max_depth is not None and depth > max_depth:
            raise ExecutionLimitExceeded(
                "reader_depth", max_depth, depth
            )
        if pos >= len(tokens):
            raise LispSyntaxError(
                "unexpected end of input",
                source_name=source_name,
                column=source_length + 1,
                incomplete=True,
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
                    f"missing closing '{close}' for '{tok}' opened at "
                    f"{tok.line}:{tok.column}",
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
                f"unexpected closing delimiter '{tok}'",
                source_name=tok.source_name,
                line=tok.line,
                column=tok.column,
            )

        # atom
        pos += 1
        return parse_atom(tok)

    while pos < len(tokens):
        expressions.append(read_expr())

    return expressions


def parse(
    source: str,
    *,
    max_depth: int | None = 256,
    source_name: str = "<string>",
) -> list:
    """Parse source string into a list of s-expressions."""
    tokens = tokenize(source, source_name=source_name)
    return _parse_tokens(
        tokens,
        source_name=source_name,
        source_length=len(source),
        max_depth=max_depth,
    )


def read_source(
    source: str,
    *,
    max_depth: int | None = 256,
    source_name: str = "<string>",
) -> ReadResult:
    """Classify and parse source for batch and interactive readers."""
    try:
        expressions = parse(
            source,
            max_depth=max_depth,
            source_name=source_name,
        )
        return ReadResult(ReadState.COMPLETE, expressions=expressions)
    except LispSyntaxError as exc:
        state = ReadState.INCOMPLETE if exc.incomplete else ReadState.INVALID
        return ReadResult(state, error=exc)
    except InvalidDataError as exc:
        return ReadResult(ReadState.INVALID, error=exc)


def parse_atom(tok: str) -> Any:
    """Parse a single atom token."""
    # booleans
    if tok == "#t":
        return True
    if tok == "#f":
        return False

    # nil
    if tok == "nil":
        return NIL

    # number (int)
    try:
        return int(tok)
    except ValueError:
        pass

    # number (float)
    try:
        value = float(tok)
        if not math.isfinite(value):
            raise InvalidDataError(f"non-finite numeric literal: {tok}")
        return value
    except ValueError:
        pass

    # string
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

    # symbol
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
    return Parameters(
        [str(param) for param in params[:dot]],
        rest=str(params[dot + 1]),
    )


# ---------------------------------------------------------------------------
# Value representation
# ---------------------------------------------------------------------------

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
        return "(" + " ".join(
            _value_repr(x, _validated=True) for x in val
        ) + ")"
    if isinstance(val, dict):
        items = " ".join(
            f"({_value_repr(k, _validated=True)} . "
            f"{_value_repr(v, _validated=True)})"
            for k, v in val.items()
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


# ---------------------------------------------------------------------------
# JSON <-> S-expression conversion
# ---------------------------------------------------------------------------

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
    raise InvalidDataError(
        f"unsupported JSON value: {type(obj).__name__}"
    )


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
    raise InvalidDataError(
        f"unsupported LisPy JSON value: {type(val).__name__}"
    )


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
                if not (
                    type(current_left) is bool
                    and type(current_right) is bool
                    and current_left == current_right
                ):
                    return False
                continue
            if isinstance(current_left, Symbol) or isinstance(
                current_right,
                Symbol,
            ):
                if not (
                    isinstance(current_left, Symbol)
                    and isinstance(current_right, Symbol)
                    and str(current_left) == str(current_right)
                ):
                    return False
                continue
            if (
                type(current_left) in (int, float)
                and type(current_right) in (int, float)
            ):
                if current_left != current_right:
                    return False
                continue
            if type(current_left) is str or type(current_right) is str:
                if not (
                    type(current_left) is str
                    and type(current_right) is str
                    and current_left == current_right
                ):
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
                pending.extend(
                    (current_left[key], current_right[key])
                    for key in current_left
                )
            elif current_left != current_right:
                return False
        return True

    @staticmethod
    def identical(left, right):
        return left is right

    @staticmethod
    def is_null(value):
        return value is NIL or value is None or (
            isinstance(value, list) and len(value) == 0
        )

    @staticmethod
    def is_pair(value):
        return isinstance(value, Pair) or (
            isinstance(value, list) and len(value) > 0
        )

    @staticmethod
    def is_list(value):
        return isinstance(value, list)

    from_json = staticmethod(json_to_lisp)
    to_json = staticmethod(lisp_to_json)

    @staticmethod
    def to_wire(value):
        return to_wire(value)


CORE1_VALUES = Core1ValueOps()


# ---------------------------------------------------------------------------
# Rappterbook bindings
# ---------------------------------------------------------------------------

def _state_path(filename: str, state_dir: str | Path | None = None) -> Path:
    """Resolve a state path without allowing escapes from STATE_DIR."""
    root = Path(state_dir if state_dir is not None else STATE_DIR).resolve()
    path = (root / filename).resolve()
    try:
        path.relative_to(root)
    except ValueError:
        raise CapabilityDenied(f"state path escapes configured root: {filename}")
    return path


def set_state_dir(path: str | Path) -> None:
    """Set the read-only state root used by rb-* bindings."""
    global STATE_DIR
    STATE_DIR = Path(path).expanduser().resolve()


def rb_state(filename: str, *, state_dir=None) -> Any:
    """Read a state JSON file and return as Lisp value."""
    path = _state_path(filename, state_dir)
    try:
        with open(path, "rb") as file:
            raw = file.read(MAX_STATE_BYTES + 1)
        if len(raw) > MAX_STATE_BYTES:
            raise InvalidDataError(
                f"state file exceeds {MAX_STATE_BYTES} bytes: {path}"
            )
        data = _strict_json_loads(raw.decode("utf-8"))
        return json_to_lisp(data)
    except FileNotFoundError:
        raise LispError(f"state file not found: {path}")
    except (UnicodeDecodeError, ValueError, json.JSONDecodeError) as e:
        raise InvalidDataError(f"invalid JSON in {path}: {e}")


def rb_agent(agent_id: str, *, state_dir=None) -> Any:
    """Get an agent profile by ID."""
    agents = rb_state("agents.json", state_dir=state_dir)
    if not isinstance(agents, dict) or not isinstance(
        agents.get("agents"), dict
    ):
        raise LispError("agents.json must contain an 'agents' object")
    agent_map = agents.get("agents", {})
    agent = agent_map.get(agent_id)
    if agent is None:
        raise LispError(f"agent not found: {agent_id}")
    result = dict(agent)
    result["id"] = agent_id
    return json_to_lisp(result)


def rb_soul(agent_id: str, *, state_dir=None) -> Any:
    """Read an agent's soul file."""
    if (
        not isinstance(agent_id, str)
        or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", agent_id)
        is None
    ):
        raise InvalidDataError("agent id is not canonical")
    path = _state_path(f"memory/{agent_id}.md", state_dir)
    try:
        with open(path, "rb") as file:
            content = file.read(MAX_STATE_BYTES + 1)
    except FileNotFoundError:
        return f"(no soul file for {agent_id})"
    if len(content) > MAX_STATE_BYTES:
        raise ExecutionLimitExceeded(
            "state_bytes",
            MAX_STATE_BYTES,
            len(content),
        )
    try:
        return content.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise InvalidDataError(f"invalid UTF-8 soul file: {exc}")


def rb_channels(*, state_dir=None) -> Any:
    """Get all channels as a list of dicts."""
    data = rb_state("channels.json", state_dir=state_dir)
    if not isinstance(data, dict) or not isinstance(
        data.get("channels"), dict
    ):
        raise LispError("channels.json must contain a 'channels' object")
    channels = data.get("channels", {})
    result = []
    for slug, ch in channels.items():
        entry = dict(ch)
        entry["slug"] = slug
        result.append(entry)
    return json_to_lisp(result)


def rb_trending(*, state_dir=None) -> Any:
    """Get trending posts."""
    data = rb_state("trending.json", state_dir=state_dir)
    if not isinstance(data, dict) or not isinstance(
        data.get("trending"), list
    ):
        raise LispError("trending.json must contain a 'trending' list")
    return json_to_lisp(data["trending"])


def _required_text(value, field):
    if (
        isinstance(value, Symbol)
        or not isinstance(value, str)
        or not value.strip()
    ):
        raise LispError(f"{field} must be a non-empty string")
    return value


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


def rb_post(channel: str, title: str, body: str) -> dict:
    """Create a typed dry-run post intent."""
    return {
        "type": "rappterbook.post.create",
        "payload": {
            "channel": _required_text(channel, "channel"),
            "title": _required_text(title, "title"),
            "body": _required_text(body, "body"),
        },
    }


def rb_comment(discussion_number: int, body: str) -> dict:
    """Create a typed dry-run comment intent."""
    if (
        isinstance(discussion_number, bool)
        or not isinstance(discussion_number, int)
        or discussion_number < 1
    ):
        raise LispError("discussion_number must be a positive integer")
    return {
        "type": "rappterbook.comment.create",
        "payload": {
            "discussion_number": discussion_number,
            "body": _required_text(body, "body"),
        },
    }


def rb_react(node_id: str, reaction: str) -> dict:
    """Create a typed dry-run reaction intent."""
    allowed = {
        "thumbs_up",
        "thumbs_down",
        "laugh",
        "hooray",
        "confused",
        "heart",
        "rocket",
        "eyes",
    }
    reaction = _required_text(reaction, "reaction").lower()
    if reaction not in allowed:
        raise LispError(f"unsupported reaction: {reaction}")
    return {
        "type": "rappterbook.reaction.add",
        "payload": {
            "node_id": _required_text(node_id, "node_id"),
            "reaction": reaction,
        },
    }


def effect_idempotency_key(contract_id, intent_scope, sequence):
    material = json.dumps(
        [contract_id, intent_scope, sequence],
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(material).hexdigest()


def effect_digest(source_hash, effect_type, payload):
    material = json.dumps(
        [source_hash, effect_type, payload],
        allow_nan=False,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(material).hexdigest()


def rb_run(code: str) -> str:
    """Execute trusted Python code via run_python.sh if available."""
    script = REPO_ROOT / "scripts" / "run_python.sh"
    try:
        if not script.exists():
            result = subprocess.run(
                [sys.executable, "-c", code],
                capture_output=True,
                text=True,
                timeout=10,
            )
        else:
            result = subprocess.run(
                ["bash", str(script), code],
                capture_output=True,
                text=True,
                timeout=10,
            )
    except subprocess.TimeoutExpired:
        raise LispError("trusted Python execution timed out")
    except OSError as exc:
        raise LispError(f"trusted Python execution failed: {exc}")

    if result.returncode != 0:
        message = result.stderr.strip() or "no error output"
        raise LispError(
            f"trusted Python exited {result.returncode}: {message}"
        )
    return result.stdout


# ---------------------------------------------------------------------------
# Evaluator
# ---------------------------------------------------------------------------

def _validated_bindings(bindings, form, *, allow_duplicates):
    if not isinstance(bindings, list):
        raise LispError(f"{form} bindings must be a list")
    names = []
    for binding in bindings:
        if (
            not isinstance(binding, list)
            or len(binding) != 2
            or not isinstance(binding[0], Symbol)
        ):
            raise LispError(f"invalid {form} binding: {binding}")
        names.append(str(binding[0]))
    if not allow_duplicates and len(names) != len(set(names)):
        raise LispError(f"duplicate {form} binding")
    return bindings


def evaluate(expr: Any, env: Env) -> Any:
    """Evaluate an s-expression in an environment."""
    env.context.consume()

    # Self-evaluating types
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

    # Symbol lookup
    if isinstance(expr, Symbol):
        return env.find(expr)[expr]

    # Not a list — return as-is
    if not isinstance(expr, list):
        return expr

    # Empty list
    if len(expr) == 0:
        return NIL

    head = expr[0]

    # Special forms
    if isinstance(head, Symbol):
        # quote
        if head == "quote":
            if len(expr) != 2:
                raise LispError("quote requires exactly 1 argument")
            return expr[1]

        if head in ("quasiquote", "unquote"):
            raise UnsupportedFormError(
                f"{head} is not supported by lispy-core@1"
            )

        # if
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

        # cond
        if head == "cond":
            clauses = expr[1:]
            for index, clause in enumerate(clauses):
                if not isinstance(clause, list) or len(clause) < 2:
                    raise LispError("invalid cond clause")
                if (
                    isinstance(clause[0], Symbol)
                    and clause[0] == "else"
                    and index != len(clauses) - 1
                ):
                    raise LispError("cond else clause must be last")
            for clause in clauses:
                if isinstance(clause[0], Symbol) and clause[0] == "else":
                    return eval_body(clause[1:], env)
                test = evaluate(clause[0], env)
                if env.value_ops.truthy(test):
                    return eval_body(clause[1:], env)
            return NIL

        # define
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

        # set!
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

        # lambda
        if head == "lambda":
            if len(expr) < 3:
                raise LispError("lambda requires params and body")
            params = _parse_parameters(expr[1])
            return Lambda(params, expr[2:], env)

        # let
        if head == "let":
            if len(expr) < 3:
                raise LispError("let requires bindings and body")
            bindings = _validated_bindings(
                expr[1], "let", allow_duplicates=False
            )
            body = expr[2:]
            new_env = Env(outer=env)
            for binding in bindings:
                name = str(binding[0])
                val = evaluate(binding[1], env)
                new_env[name] = val
            return eval_body(body, new_env)

        # let*
        if head == "let*":
            if len(expr) < 3:
                raise LispError("let* requires bindings and body")
            bindings = _validated_bindings(
                expr[1], "let*", allow_duplicates=True
            )
            body = expr[2:]
            new_env = Env(outer=env)
            for binding in bindings:
                name = str(binding[0])
                val = evaluate(binding[1], new_env)
                new_env[name] = val
            return eval_body(body, new_env)

        # begin
        if head == "begin":
            return eval_body(expr[1:], env)

        # and
        if head == "and":
            result: Any = True
            for arg in expr[1:]:
                result = evaluate(arg, env)
                if not env.value_ops.truthy(result):
                    return result
            return result

        # or
        if head == "or":
            for arg in expr[1:]:
                result = evaluate(arg, env)
                if env.value_ops.truthy(result):
                    return result
            return False

        # define-macro
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
            raise UnsupportedFormError(
                "do is not supported by lispy-core@1"
            )

    # Function application
    fn = evaluate(head, env)

    # Macro expansion
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

    # Normal function call
    args = [evaluate(arg, env) for arg in expr[1:]]

    return _call_fn(fn, args, env.context, call_name=head)


def eval_body(body: list, env: Env) -> Any:
    """Evaluate a sequence of expressions, return the last result."""
    result: Any = NIL
    for expr in body:
        result = evaluate(expr, env)
    return result


# ---------------------------------------------------------------------------
# Standard library (built-in functions)
# ---------------------------------------------------------------------------

def _denied(name: str):
    def deny(*_args):
        raise CapabilityDenied(
            f"capability denied: {name}; rerun the CLI with --trusted"
        )

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
    source_name = (
        str(fallback)
        if fallback.exists()
        else f"<package:{resource}>"
    )
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
        writer = BoundedWriter(
            sys.stdout.write,
            context.limits.max_output_bytes,
        )
    elif isinstance(output, (BoundedOutput, BoundedWriter)):
        writer = output
    else:
        target = output if callable(output) else output.write
        writer = BoundedWriter(
            target,
            context.limits.max_output_bytes,
        )
    env = Env(
        context=context,
        writer=writer,
    )
    state_root = Path(
        state_dir if state_dir is not None else STATE_DIR
    ).expanduser().resolve()

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
    env["expt"] = lambda a, b: _finite_result(
        pow(_real_number(a), _real_number(b))
    )
    env["pow"] = env["expt"]
    env["sqrt"] = lambda value: _finite_result(
        math.sqrt(_real_number(value))
    )
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
    env["cons"] = lambda a, b: Pair(a, b)
    env["car"] = lambda x: _car(x)
    env["cdr"] = lambda x: _cdr(x)
    env["list"] = lambda *args: _list_fn(env.context, *args)
    env["length"] = lambda value: _length_fn(env.context, value)
    env["append"] = lambda *lists: _append(
        *lists, context=env.context
    )
    env["reverse"] = lambda value: _reverse_fn(env.context, value)
    env["nth"] = lambda values, index: _nth_fn(
        env.context, values, index
    )
    env["take"] = lambda values, count: _slice_fn(
        env.context, values, None, count
    )
    env["drop"] = lambda values, count: _slice_fn(
        env.context, values, count, None
    )
    env["range"] = lambda *args: _range_fn(env.context, *args)
    env["zip"] = lambda *lists: _zip_fn(env.context, *lists)
    env["flatten"] = lambda values: _flatten(
        values, context=env.context
    )
    env["sort"] = lambda values, *key_fn: _sort_fn(
        values, *key_fn, context=env.context
    )
    env["first"] = lambda x: _car(x)
    env["rest"] = lambda x: _cdr(x)
    env["last"] = lambda values: _last_fn(env.context, values)
    env["empty?"] = lambda x: x is NIL or x is None or (isinstance(x, (list, dict, str)) and len(x) == 0)

    # -- Higher-order functions --
    env["map"] = lambda fn, *lists: _map_fn(
        fn, *lists, context=env.context
    )
    env["filter"] = lambda fn, values: _filter_fn(
        fn, values, context=env.context
    )
    env["reduce"] = lambda fn, values, *initial: _reduce_fn(
        fn, values, *initial, context=env.context
    )
    env["for-each"] = lambda fn, values: _for_each_fn(
        fn, values, context=env.context
    )
    env["apply"] = lambda fn, args: _apply_fn(
        fn, args, context=env.context
    )
    env["compose"] = lambda f, g: lambda *args: _call_fn(
        f, [_call_fn(g, list(args))]
    )

    # -- String operations --
    env["string-append"] = lambda *args: _bounded_string(
        env.context,
        "".join(str(a) for a in args),
    )
    env["concat"] = env["string-append"]
    env["string-length"] = lambda s: len(s)
    env["substring"] = lambda s, start, *end: _bounded_string(
        env.context,
        s[start:end[0]] if end else s[start:],
    )
    env["string-upcase"] = lambda s: _bounded_string(env.context, s.upper())
    env["string-downcase"] = lambda s: _bounded_string(env.context, s.lower())
    env["string-contains?"] = lambda s, sub: sub in s
    env["string-split"] = lambda s, *delim: _string_split(
        env.context, s, *delim
    )
    env["string-join"] = lambda values, *separator: _string_join(
        env.context,
        values,
        *separator,
    )
    env["string-trim"] = lambda s: _bounded_string(env.context, s.strip())
    env["string-replace"] = lambda s, old, new: _bounded_string(
        env.context,
        s.replace(old, new),
    )
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
    env["keys"] = lambda value: _dict_items(
        env.context, value, keys=True
    )
    env["values"] = lambda value: _dict_items(
        env.context, value, keys=False
    )
    env["has-key?"] = _has_key
    env["dict-set"] = lambda d, k, v: _dict_set(
        env.context,
        d,
        k,
        v,
    )
    env["dict-merge"] = lambda *dicts: _merge_dicts(
        *dicts,
        context=env.context,
    )
    env["dict-map"] = lambda fn, value: _dict_map_fn(
        fn, value, context=env.context
    )
    env["dict-filter"] = lambda fn, value: _dict_filter_fn(
        fn, value, context=env.context
    )
    env["make-dict"] = lambda *pairs: _make_dict(env.context, *pairs)

    # -- I/O --
    env["display"] = lambda *args: _display(env.writer, *args)
    env["newline"] = lambda: _newline(env.writer)
    env["print"] = lambda x: _print_val(env.writer, x)
    env["println"] = lambda x: _println_val(env.writer, x)
    env["log"] = lambda *args: _println_val(
        env.writer, " ".join(display_value(x) for x in args)
    )

    if host_profile:
        if trusted:
            env["read-file"] = lambda path: _read_file(path)
            env["write-file"] = lambda path, content: _write_file(path, content)
            env["file-exists?"] = lambda path: Path(path).exists()
        else:
            env["read-file"] = _denied("filesystem.read")
            env["write-file"] = _denied("filesystem.write")
            env["file-exists?"] = _denied("filesystem.read")

    # -- JSON --
    env["json-parse"] = lambda source: _json_parse(
        source,
        context=env.context,
    )
    env["json->sexp"] = env["json-parse"]
    env["json-dump"] = lambda val: _bounded_string(
        env.context,
        json.dumps(
            lisp_to_json(val),
            indent=2,
            allow_nan=False,
        ),
    )
    env["sexp->json"] = env["json-dump"]

    # -- Rappterbook bindings --
    if host_profile:
        env["rb-state"] = lambda filename: rb_state(
            filename, state_dir=state_root
        )
        env["rb-agent"] = lambda agent_id: rb_agent(
            agent_id, state_dir=state_root
        )
        env["rb-soul"] = lambda agent_id: rb_soul(
            agent_id, state_dir=state_root
        )
        env["rb-channels"] = lambda: rb_channels(state_dir=state_root)
        env["rb-trending"] = lambda: rb_trending(state_dir=state_root)
        env["rb-post"] = rb_post
        env["rb-comment"] = rb_comment
        env["rb-react"] = rb_react
        env["rb-run"] = rb_run if trusted else _denied("process.python")

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
        capabilities.extend(
            ["filesystem.read", "filesystem.write", "process.python"]
        )
    env["capabilities"] = lambda: list(capabilities)
    env["runtime-info"] = lambda: {
        "name": "LisPy",
        "version": VERSION,
        "profile": LANGUAGE_PROFILE,
        "profiles": (
            [LANGUAGE_PROFILE]
            if not host_profile
            else [
                LANGUAGE_PROFILE,
                "rappterbook.read",
                "rappterbook.plan",
            ]
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


# ---------------------------------------------------------------------------
# Helper functions for built-ins
# ---------------------------------------------------------------------------

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
        raise LispError(
            f"expected proper list, got {_value_repr(value)}"
        )
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
    normalized = [
        _proper_list_view(value, context=context) for value in lists
    ]
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
    normalized = [
        _proper_list_view(value, context=context) for value in lists
    ]
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
    max_depth = (
        context.limits.max_reader_depth if context is not None else 256
    )
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
                raise ExecutionLimitExceeded(
                    "collection_depth", max_depth, depth
                )
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
        if bound is not None and (
            isinstance(bound, bool)
            or not isinstance(bound, int)
            or bound < 0
        ):
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
    return _bounded_string(
        context,
        (separator[0] if separator else " ").join(
            str(item) for item in items
        ),
    )


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
    raise LispError(
        "sort without comparator requires homogeneous numbers or strings"
    )


def _map_fn(fn, *lists, context=None):
    if not lists:
        raise LispError("map requires a function and at least one list")
    normalized = [
        _proper_list_view(value, context=context) for value in lists
    ]
    if len(normalized) == 1:
        return [
            _call_fn(fn, [item], context)
            for item in normalized[0]
        ]
    min_len = min(len(value) for value in normalized)
    if context is not None:
        context.check_collection(min_len)
    return [
        _call_fn(fn, [value[i] for value in normalized], context)
        for i in range(min_len)
    ]


def _filter_fn(fn, lst, *, context=None):
    lst = _proper_list_view(lst, context=context)
    if context is not None:
        context.check_collection(len(lst))
    return [
        x for x in lst
        if _is_truthy(_call_fn(fn, [x], context))
    ]


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
        except (
            AttributeError,
            ArithmeticError,
            IndexError,
            KeyError,
            OSError,
            TypeError,
            ValueError,
        ) as exc:
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
    return {
        k: _call_fn(fn, [k, v], context)
        for k, v in d.items()
    }


def _dict_filter_fn(fn, d, *, context=None):
    d = _validated_dict(d)
    if context is not None:
        context.check_collection(len(d))
    return {
        k: v
        for k, v in d.items()
        if _is_truthy(_call_fn(fn, [k, v], context))
    }


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


def _read_file(path):
    try:
        with open(path, "r") as f:
            return f.read()
    except FileNotFoundError:
        raise LispError(f"file not found: {path}")


def _write_file(path, content):
    with open(path, "w") as f:
        f.write(str(content))
    return True


# ---------------------------------------------------------------------------
# REPL
# ---------------------------------------------------------------------------

def repl(env: Env | None = None):
    """Run an interactive REPL."""
    if env is None:
        env = make_global_env()

    print(f"LisPy v{VERSION} ({LANGUAGE_PROFILE})")
    print("Code is data. Data is code. The REPL is the heartbeat.")
    print('Type (help) for available commands, or Ctrl-D to exit.\n')

    env["help"] = lambda: _repl_help()

    buffer = ""
    while True:
        try:
            prompt = "...  " if buffer else "\u03bb> "
            line = input(prompt)
        except EOFError:
            if buffer:
                result = read_source(
                    buffer,
                    max_depth=env.context.limits.max_reader_depth,
                    source_name="<repl>",
                )
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
            read_result = read_source(
                buffer,
                max_depth=env.context.limits.max_reader_depth,
                source_name="<repl>",
            )
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


# ---------------------------------------------------------------------------
# Script runner
# ---------------------------------------------------------------------------

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
        raise LispSyntaxError(
            f"script is not valid UTF-8: {exc}",
            source_name=str(path),
        )
    return run_string(source, env, source_name=str(path))


def run_string(
    source: str,
    env: Env | None = None,
    *,
    source_name: str = "<string>",
):
    """Execute a Lisp string."""
    if env is None:
        env = make_global_env()

    env.context.reset()
    if hasattr(env.writer, "reset"):
        env.writer.reset()
    env.context.check_source(source)
    exprs = parse(
        source,
        max_depth=env.context.limits.max_reader_depth,
        source_name=source_name,
    )
    result = NIL
    for expr in exprs:
        result = evaluate(expr, env)
    _validate_value_graph(
        result,
        max_depth=env.context.limits.max_reader_depth,
        max_nodes=100_000,
    )
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
        self.state_root = Path(
            state_root if state_root is not None else STATE_DIR
        ).expanduser().resolve()
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
            _validate_value_graph(
                value,
                max_depth=env.context.limits.max_reader_depth,
                max_nodes=100_000,
            )
            to_wire(value)
            return ExecutionResult(
                ok=True,
                value=value,
                output=output.getvalue(),
                error=None,
                usage={
                    "steps": env.context.steps,
                    "peak_call_depth": env.context.peak_call_depth,
                },
            )
        except LispSyntaxError as exc:
            phase = "parse"
            error = exc
        except LispError as exc:
            phase = "evaluate"
            error = exc

        details = {
            "phase": phase,
            "type": type(error).__name__,
            "category": error.category,
            "message": str(error),
        }
        if isinstance(error, ExecutionLimitExceeded):
            details.update(
                {
                    "resource": error.resource,
                    "limit": error.limit,
                    "used": error.used,
                }
            )
        if isinstance(error, LispSyntaxError):
            details["location"] = {
                "source": error.source_name,
                "line": error.line,
                "column": error.column,
            }
        return ExecutionResult(
            ok=False,
            value=NIL,
            output=output.getvalue(),
            error=details,
            usage={
                "steps": env.context.steps,
                "peak_call_depth": env.context.peak_call_depth,
            },
        )


def _copy_host_value(
    value: Any,
    path: str = "$",
    seen: set[int] | None = None,
) -> Any:
    """Validate and copy a JSON-compatible host value."""
    if seen is None:
        seen = set()
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(f"{path}: non-finite numbers are not supported")
        return value
    if isinstance(value, list):
        identity = id(value)
        if identity in seen:
            raise ValueError(f"{path}: cyclic host value")
        seen.add(identity)
        try:
            return [
                _copy_host_value(item, f"{path}[{index}]", seen)
                for index, item in enumerate(value)
            ]
        finally:
            seen.remove(identity)
    if isinstance(value, dict):
        identity = id(value)
        if identity in seen:
            raise ValueError(f"{path}: cyclic host value")
        seen.add(identity)
        copied = {}
        try:
            for key, item in value.items():
                if not isinstance(key, str):
                    raise TypeError(f"{path}: object keys must be strings")
                copied[key] = _copy_host_value(item, f"{path}.{key}", seen)
            return copied
        finally:
            seen.remove(identity)
    raise TypeError(f"{path}: unsupported host value {type(value).__name__}")


def _receipt_value(
    value: Any,
    path: str = "$",
    *,
    _validated=False,
) -> Any:
    """Strictly convert a LisPy value into a JSON-compatible receipt value."""
    if not _validated:
        _validate_value_graph(value)
    if value is NIL or value is None:
        return None
    if isinstance(value, Symbol):
        raise TypeError(f"{path}: symbols are not JSON receipt values")
    if isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(f"{path}: non-finite numbers are not supported")
        return value
    if isinstance(value, Pair):
        items = []
        current = value
        while isinstance(current, Pair):
            items.append(
                _receipt_value(
                    current.car,
                    f"{path}[{len(items)}]",
                    _validated=True,
                )
            )
            current = current.cdr
        if current is not NIL:
            raise TypeError(f"{path}: improper lists are not JSON receipt values")
        return items
    if isinstance(value, list):
        return [
            _receipt_value(
                item,
                f"{path}[{index}]",
                _validated=True,
            )
            for index, item in enumerate(value)
        ]
    if isinstance(value, dict):
        converted = {}
        for key, item in value.items():
            if not isinstance(key, str) or isinstance(key, Symbol):
                raise TypeError(f"{path}: object keys must be strings")
            converted[key] = _receipt_value(
                item,
                f"{path}.{key}",
                _validated=True,
            )
        return converted
    raise TypeError(f"{path}: unsupported LisPy value {type(value).__name__}")


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
                return {
                    "tag": "pair",
                    "car": to_wire(value.car, _seen=_seen),
                    "cdr": to_wire(value.cdr, _seen=_seen),
                }
            if type(value) is list:
                return {
                    "tag": "list",
                    "items": [
                        to_wire(item, _seen=_seen) for item in value
                    ],
                }
            entries = [
                [
                    to_wire(key, _seen=_seen),
                    to_wire(item, _seen=_seen),
                ]
                for key, item in value.items()
            ]
            entries.sort(
                key=lambda entry: json.dumps(
                    entry[0],
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            )
            encoded_keys = [
                json.dumps(
                    entry[0],
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
                for entry in entries
            ]
            if len(encoded_keys) != len(set(encoded_keys)):
                raise WireEncodingError(
                    "map contains duplicate portable keys"
                )
            return {"tag": "map", "entries": entries}
        finally:
            _seen.remove(identity)

    raise WireEncodingError(
        f"unsupported portable value: {type(value).__name__}"
    )


def _hosted_error_receipt(
    base,
    output,
    phase,
    error,
    *,
    context=None,
):
    details = {
        "phase": phase,
        "type": type(error).__name__,
        "category": getattr(error, "category", "host"),
        "message": str(error),
    }
    if isinstance(error, ExecutionLimitExceeded):
        details.update(
            {
                "resource": error.resource,
                "limit": error.limit,
                "used": error.used,
            }
        )
    if isinstance(error, LispSyntaxError):
        details["location"] = {
            "source": error.source_name,
            "line": error.line,
            "column": error.column,
        }
    receipt = {
        **base,
        "status": "rolled_back",
        "outputs": None,
        "writes": [],
        "result": None,
        "logs": output.getvalue(),
        "usage": (
            {
                "steps": context.steps,
                "peak_call_depth": context.peak_call_depth,
            }
            if context is not None
            else None
        ),
        "effects": [],
        "error": details,
    }
    json.dumps(receipt, allow_nan=False)
    return receipt


def run_hosted_governor(
    source: str,
    *,
    inputs: dict[str, Any],
    mutable_outputs: dict[str, Any],
    validate=None,
    contract_id: str = "hosted-governor@1",
    limits: ExecutionLimits | None = None,
    intent_scope: str | None = None,
    validate_effects=None,
    max_effects: int = 100,
    source_id: str | None = None,
) -> dict[str, Any]:
    """Run a governor transactionally and return a JSON-safe receipt."""
    source_hash = hashlib.sha256(source.encode("utf-8")).hexdigest()
    base = {
        "api": "lispy.hosted-governor/v2",
        "runtime": {
            "version": VERSION,
            "profile": LANGUAGE_PROFILE,
        },
        "contract_id": contract_id,
        "source_sha256": source_hash,
        "source_id": source_id,
        "intent_scope": intent_scope,
    }
    effective_limits = limits or ExecutionLimits()
    output = BoundedOutput(effective_limits.max_output_bytes)
    effects = []

    try:
        if intent_scope is not None:
            _required_text(intent_scope, "intent_scope")
        if (
            isinstance(max_effects, bool)
            or not isinstance(max_effects, int)
            or max_effects < 1
        ):
            raise ValueError("max_effects must be a positive integer")
        if not isinstance(inputs, dict):
            raise TypeError("inputs must be an object")
        if not isinstance(mutable_outputs, dict):
            raise TypeError("mutable_outputs must be an object")
        copied_inputs = _copy_host_value(inputs, "$.inputs")
        copied_outputs = _copy_host_value(mutable_outputs, "$.outputs")
        for name in (*copied_inputs, *copied_outputs):
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_-]*", name):
                raise ValueError(f"invalid host binding name: {name!r}")
        overlap = set(copied_inputs) & set(copied_outputs)
        if overlap:
            raise ValueError(
                "input/output bindings overlap: " + ", ".join(sorted(overlap))
            )
    except (TypeError, ValueError) as exc:
        return _hosted_error_receipt(base, output, "input", exc)

    try:
        env = make_global_env(
            trusted=False,
            limits=effective_limits,
            output=output,
            profile="core",
        )
    except LispError as exc:
        return _hosted_error_receipt(base, output, "prepare", exc)

    hosted_capabilities = [
        "lispy-core@1",
        "console.capture",
        "hosted-governor@2",
    ]
    for name in (
        "read-file",
        "write-file",
        "file-exists?",
        "rb-state",
        "rb-agent",
        "rb-soul",
        "rb-channels",
        "rb-trending",
        "rb-post",
        "rb-comment",
        "rb-react",
        "rb-run",
    ):
        env[name] = _denied("hosted-governor")
    env["capabilities"] = lambda: list(hosted_capabilities)
    env["runtime-info"] = lambda: {
        "name": "LisPy",
        "version": VERSION,
        "profile": LANGUAGE_PROFILE,
        "profiles": [
            LANGUAGE_PROFILE,
            "hosted-governor@2",
        ],
        "host_api": "lispy.hosted-governor/v2",
        "trusted": False,
        "stdlib_loaded": True,
        "capabilities": list(hosted_capabilities),
        "limits": env.context.limits.as_dict(),
    }
    if intent_scope is not None:
        hosted_capabilities.append("rappterbook.effects.dry-run@1")

        def collect(intent):
            sequence = len(effects)
            if sequence >= max_effects:
                raise ExecutionLimitExceeded(
                    "effects", max_effects, sequence + 1
                )
            effect = {
                "sequence": sequence,
                "type": intent["type"],
                "payload": _copy_host_value(intent["payload"]),
                "mode": "dry_run",
                "applied": False,
                "idempotency_key": effect_idempotency_key(
                    contract_id,
                    intent_scope,
                    sequence,
                ),
                "effect_sha256": effect_digest(
                    source_hash,
                    intent["type"],
                    intent["payload"],
                ),
            }
            effects.append(effect)
            return effect

        env["rb-post"] = lambda channel, title, body: collect(
            rb_post(channel, title, body)
        )
        env["rb-comment"] = lambda number, body: collect(
            rb_comment(number, body)
        )
        env["rb-react"] = lambda node_id, reaction: collect(
            rb_react(node_id, reaction)
        )

    existing = set(env)
    collisions = existing & (set(copied_inputs) | set(copied_outputs))
    if collisions:
        error = ValueError(
            "host bindings collide with runtime names: "
            + ", ".join(sorted(collisions))
        )
        return _hosted_error_receipt(
            base, output, "input", error, context=env.context
        )

    for name, value in copied_inputs.items():
        env[name] = py_to_lisp(value)
    for name, value in copied_outputs.items():
        env[name] = py_to_lisp(value)

    env.readonly.update(existing)
    env.readonly.update(copied_inputs)
    env.protected_definitions.update(existing)
    env.protected_definitions.update(copied_inputs)
    env.protected_definitions.update(copied_outputs)
    env.writable_outputs.update(copied_outputs)

    try:
        result = run_string(
            source,
            env,
            source_name=f"<hosted:{contract_id}>",
        )
        outputs = {
            name: _receipt_value(env[name], f"$.outputs.{name}")
            for name in copied_outputs
        }
        converted_result = _receipt_value(result, "$.result")
    except LispSyntaxError as exc:
        return _hosted_error_receipt(
            base, output, "parse", exc, context=env.context
        )
    except LispError as exc:
        return _hosted_error_receipt(
            base, output, "evaluate", exc, context=env.context
        )
    except (TypeError, ValueError) as exc:
        return _hosted_error_receipt(
            base, output, "serialize", exc, context=env.context
        )

    if validate is not None:
        try:
            violations = validate(
                _copy_host_value(copied_inputs),
                _copy_host_value(outputs),
            )
            if violations:
                if isinstance(violations, str):
                    violations = [violations]
                raise HostedValidationError(violations)
        except HostedValidationError as exc:
            return _hosted_error_receipt(
                base, output, "validate", exc, context=env.context
            )
        except LispError as exc:
            return _hosted_error_receipt(
                base, output, "validate", exc, context=env.context
            )
        except (
            AttributeError,
            KeyError,
            RuntimeError,
            TypeError,
            ValueError,
        ) as exc:
            return _hosted_error_receipt(
                base, output, "validate", exc, context=env.context
            )

    if validate_effects is not None:
        try:
            violations = validate_effects(_copy_host_value(effects))
            if violations:
                if isinstance(violations, str):
                    violations = [violations]
                raise HostedValidationError(violations)
        except HostedValidationError as exc:
            return _hosted_error_receipt(
                base, output, "validate", exc, context=env.context
            )
        except LispError as exc:
            return _hosted_error_receipt(
                base, output, "validate", exc, context=env.context
            )
        except (
            AttributeError,
            KeyError,
            RuntimeError,
            TypeError,
            ValueError,
        ) as exc:
            return _hosted_error_receipt(
                base, output, "validate", exc, context=env.context
            )

    receipt = {
        **base,
        "status": "accepted",
        "outputs": outputs,
        "writes": sorted(env.tracked_writes),
        "result": converted_result,
        "logs": output.getvalue(),
        "usage": {
            "steps": env.context.steps,
            "peak_call_depth": env.context.peak_call_depth,
        },
        "effects": effects,
        "error": None,
    }
    json.dumps(receipt, allow_nan=False)
    return receipt


WORKER_API = "lispy.worker/v1"
WORKER_MAX_LINE_BYTES = 2_097_152
WORKER_MAX_RESPONSE_BYTES = 2_097_152
WORKER_MAX_STDERR_BYTES = 65_536
WORKER_TIMEOUT_SECONDS = 5
WORKER_JSON_MAX_DEPTH = 64
WORKER_JSON_MAX_NODES = 100_000
DOCTOR_V3_MAX_REPORT_BYTES = 65_536


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


def _protocol_error(request_id, code, message):
    return {
        "api": WORKER_API,
        "id": request_id,
        "ok": False,
        "error": {
            "code": code,
            "message": str(message),
        },
    }


def _validate_worker_response_shape(response, request_id, operation):
    if not isinstance(response, dict):
        return False
    if response.get("api") != WORKER_API or response.get("id") != request_id:
        return False
    if type(response.get("ok")) is not bool:
        return False
    if response["ok"]:
        payload = "manifest" if operation == "manifest" else "receipt"
        if set(response) != {"api", "id", "ok", payload}:
            return False
        value = response[payload]
        if not isinstance(value, dict):
            return False
        if payload == "manifest":
            return set(value) == {
                "api",
                "runtime",
                "artifact_sha256",
                "operations",
                "limits",
                "contracts",
                "sources",
            }
        return (
            value.get("api") == "lispy.hosted-governor/v2"
            and value.get("status") in ("accepted", "rolled_back")
            and isinstance(value.get("source_sha256"), str)
            and isinstance(value.get("artifact_sha256"), str)
            and isinstance(value.get("request_sha256"), str)
            and isinstance(value.get("outcome_sha256"), str)
        )
    if set(response) != {"api", "id", "ok", "error"}:
        return False
    error = response["error"]
    return (
        isinstance(error, dict)
        and set(error) == {"code", "message"}
        and isinstance(error["code"], str)
        and isinstance(error["message"], str)
        and len(error["code"].encode("utf-8")) <= 128
        and len(error["message"].encode("utf-8")) <= 1024
    )


def _validate_request_fields(request, allowed, required):
    unknown = set(request) - set(allowed)
    missing = set(required) - set(request)
    if unknown:
        raise ValueError("unknown fields: " + ", ".join(sorted(unknown)))
    if missing:
        raise ValueError("missing fields: " + ", ".join(sorted(missing)))


def _worker_limits(requested):
    ceilings = ExecutionLimits()
    if requested is None:
        return ceilings
    if not isinstance(requested, dict):
        raise TypeError("limits must be an object")
    allowed = set(ceilings.as_dict())
    unknown = set(requested) - allowed
    if unknown:
        raise ValueError(
            "unknown limit fields: " + ", ".join(sorted(unknown))
        )
    values = ceilings.as_dict()
    for name, value in requested.items():
        ceiling = values[name]
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise ValueError(f"{name} must be a positive integer")
        if ceiling is not None and value > ceiling:
            raise ValueError(f"{name} exceeds supervisor ceiling {ceiling}")
        values[name] = value
    return ExecutionLimits(**values)


def _validate_mars_governor(_inputs, outputs):
    required = {
        "heating_alloc",
        "isru_alloc",
        "greenhouse_alloc",
        "food_ration",
    }
    if set(outputs) != required:
        return ["Mars governor outputs must match the registered contract"]
    for name, value in outputs.items():
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
        ):
            return [f"{name} must be a finite number"]
    if any(
        not 0 <= outputs[name] <= 1
        for name in ("heating_alloc", "isru_alloc", "greenhouse_alloc")
    ):
        return ["allocation values must be between 0 and 1"]
    if not 0.08 <= outputs["food_ration"] <= 1.5:
        return ["food_ration must be between 0.08 and 1.5"]
    total = sum(
        outputs[name]
        for name in ("heating_alloc", "isru_alloc", "greenhouse_alloc")
    )
    if abs(total - 1.0) > 1e-9:
        return ["heating/isru/greenhouse allocations must sum to 1"]
    return []


def _registered_sources():
    definitions = {
        "mars-barn/governor-example": {
            "path": REPO_ROOT / "examples" / "mars-barn" / "mars-colony-governor.lisp",
            "resource": "registered/mars-colony-governor.lisp",
            "profile": "mars-governor-candidate@2",
            "contract_id": "mars-barn/governor-controls@1",
            "required_inputs": [
                "sol",
                "o2_days",
                "h2o_days",
                "food_days",
                "power_kwh",
                "colony_risk_index",
            ],
            "effect_types": [],
        },
        "lispy/hosted-doctor@1": {
            "path": REPO_ROOT / "examples" / "hosted-governor.lisp",
            "resource": "registered/hosted-governor.lisp",
            "profile": "hosted-governor@2",
            "contract_id": "mars-barn/governor-controls@1",
            "required_inputs": ["sol"],
            "effect_types": ["rappterbook.post.create"],
        },
    }
    registered = {}
    for source_id, definition in definitions.items():
        source = _resource_text(
            definition["resource"],
            definition["path"],
        )
        registered[source_id] = {
            **{
                key: value
                for key, value in definition.items()
                if key not in ("path", "resource")
            },
            "source": source,
            "source_sha256": hashlib.sha256(
                source.encode("utf-8")
            ).hexdigest(),
        }
    return registered


def registered_source(source_id):
    registered = _registered_sources()
    if source_id not in registered:
        raise InvalidDataError(f"unknown registered source: {source_id}")
    return _copy_host_value(registered[source_id])


def run_registered_governor(
    source_id,
    *,
    expected_source_sha256,
    inputs,
    mutable_outputs,
    intent_scope=None,
    limits=None,
):
    source = registered_source(source_id)
    if (
        not isinstance(expected_source_sha256, str)
        or re.fullmatch(r"[0-9a-f]{64}", expected_source_sha256) is None
    ):
        raise InvalidDataError(
            "expected_source_sha256 must be lowercase SHA-256"
        )
    if source["source_sha256"] != expected_source_sha256:
        raise InvalidDataError("registered source SHA-256 mismatch")
    if not isinstance(inputs, dict):
        raise InvalidDataError("registered governor inputs must be an object")
    missing = set(source["required_inputs"]) - set(inputs)
    if missing:
        raise InvalidDataError(
            "registered governor missing inputs: "
            + ", ".join(sorted(missing))
        )
    receipt = run_hosted_governor(
        source["source"],
        inputs=inputs,
        mutable_outputs=mutable_outputs,
        validate=_validate_mars_governor,
        contract_id=source["contract_id"],
        limits=limits,
        intent_scope=intent_scope,
        source_id=source_id,
    )
    unexpected_effects = {
        effect["type"]
        for effect in receipt.get("effects", [])
        if effect.get("type") not in source["effect_types"]
    }
    if unexpected_effects:
        raise InvalidDataError(
            "registered governor emitted forbidden effects: "
            + ", ".join(sorted(unexpected_effects))
        )
    return receipt


def _artifact_sha256(registered=None):
    registered = registered or _registered_sources()
    components = {
        "runtime": Path(__file__).read_bytes(),
        "stdlib": _resource_bytes("stdlib.lisp", STDLIB_PATH),
        "validator": b"mars-governor-validator@1",
    }
    for source_id, item in registered.items():
        components[f"source:{source_id}"] = item["source"].encode("utf-8")
    digest = hashlib.sha256()
    for name in sorted(components):
        name_bytes = name.encode("utf-8")
        content = components[name]
        digest.update(b"lispy-artifact-v1\0")
        digest.update(len(name_bytes).to_bytes(8, "big"))
        digest.update(name_bytes)
        digest.update(len(content).to_bytes(8, "big"))
        digest.update(content)
    return digest.hexdigest()


def _worker_manifest():
    registered = _registered_sources()
    return {
        "api": WORKER_API,
        "runtime": {
            "name": "LisPy",
            "version": VERSION,
            "profile": LANGUAGE_PROFILE,
            "profiles": [
                LANGUAGE_PROFILE,
                "hosted-governor@2",
            ],
        },
        "artifact_sha256": _artifact_sha256(registered),
        "operations": ["manifest", "hosted-governor"],
        "limits": ExecutionLimits().as_dict(),
        "contracts": ["mars-barn/governor-controls@1"],
        "sources": [
            {
                "id": source_id,
                "profile": item["profile"],
                "contract_id": item["contract_id"],
                "source_sha256": item["source_sha256"],
            }
            for source_id, item in registered.items()
        ],
    }


def _handle_worker_request(request):
    if not isinstance(request, dict):
        return _protocol_error(None, "invalid_request", "request must be an object")
    request_id = request.get("id")
    if (
        not isinstance(request_id, str)
        or not request_id
        or len(request_id.encode("utf-8")) > 256
    ):
        return _protocol_error(None, "invalid_request", "id must be 1-256 UTF-8 bytes")
    if request.get("api") != WORKER_API:
        return _protocol_error(request_id, "invalid_api", "unsupported worker API")

    operation = request.get("op")
    try:
        if operation == "manifest":
            _validate_request_fields(
                request,
                allowed={"api", "id", "op"},
                required={"api", "id", "op"},
            )
            return {
                "api": WORKER_API,
                "id": request_id,
                "ok": True,
                "manifest": _worker_manifest(),
            }

        if operation != "hosted-governor":
            raise ValueError(f"unsupported operation: {operation}")

        _validate_request_fields(
            request,
            allowed={
                "api",
                "id",
                "op",
                "source_id",
                "source",
                "expected_source_sha256",
                "inputs",
                "mutable_outputs",
                "contract_id",
                "limits",
                "intent_scope",
            },
            required={
                "api",
                "id",
                "op",
                "inputs",
                "mutable_outputs",
                "contract_id",
                "expected_source_sha256",
            },
        )
        contract_id = request["contract_id"]
        if contract_id != "mars-barn/governor-controls@1":
            raise ValueError(f"unknown contract: {contract_id}")
        if not isinstance(request["inputs"], dict):
            raise TypeError("inputs must be an object")
        if not isinstance(request["mutable_outputs"], dict):
            raise TypeError("mutable_outputs must be an object")
        source_id = request.get("source_id")
        if source_id is None:
            raise ValueError(
                "registered source_id is required for this contract"
            )
        if "source" in request:
            raise ValueError(
                "inline source is not allowed with registered source_id"
            )
        registered = _registered_sources()
        if source_id not in registered:
            raise ValueError(f"unknown source_id: {source_id}")
        source_entry = registered[source_id]
        if source_entry["contract_id"] != contract_id:
            raise ValueError("source contract mismatch")
        expected_hash = request.get("expected_source_sha256")
        if (
            not isinstance(expected_hash, str)
            or not re.fullmatch(r"[0-9a-f]{64}", expected_hash)
        ):
            raise ValueError("expected_source_sha256 must be lowercase SHA-256")
        if expected_hash != source_entry["source_sha256"]:
            raise ValueError("source SHA-256 mismatch")
        limits = _worker_limits(request.get("limits"))
        receipt = run_registered_governor(
            source_id,
            expected_source_sha256=expected_hash,
            inputs=request["inputs"],
            mutable_outputs=request["mutable_outputs"],
            limits=limits,
            intent_scope=request.get("intent_scope"),
        )
        receipt["artifact_sha256"] = _artifact_sha256(registered)
        receipt["request_sha256"] = "sha256:" + _canonical_sha256(
            {
                key: value
                for key, value in request.items()
                if key != "id"
            }
        )
        receipt["outcome_sha256"] = "sha256:" + _canonical_sha256(
            receipt
        )
        return {
            "api": WORKER_API,
            "id": request_id,
            "ok": True,
            "receipt": receipt,
        }
    except (
        AttributeError,
        KeyError,
        LispError,
        OSError,
        RuntimeError,
        subprocess.TimeoutExpired,
        TypeError,
        ValueError,
    ) as exc:
        return _protocol_error(request_id, "invalid_request", exc)


def _json_line(value):
    return (
        json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
        )
        + "\n"
    )


def _worker_environment(temp_root=None):
    environment = {
        "PYTHONIOENCODING": "utf-8",
        "PYTHONUTF8": "1",
    }
    for name in ("SYSTEMROOT", "WINDIR"):
        if name in os.environ:
            environment[name] = os.environ[name]
    if temp_root is not None:
        for name in ("HOME", "USERPROFILE", "TMPDIR", "TEMP", "TMP", "STATE_DIR"):
            environment[name] = str(temp_root)
    return environment


def _run_worker_once():
    line = sys.stdin.buffer.readline(WORKER_MAX_LINE_BYTES + 1)
    if len(line) > WORKER_MAX_LINE_BYTES:
        response = _protocol_error(None, "line_too_large", "request line too large")
    else:
        try:
            request = _strict_json_loads(line.decode("utf-8"))
            response = _handle_worker_request(request)
        except (UnicodeDecodeError, ValueError, json.JSONDecodeError) as exc:
            response = _protocol_error(None, "invalid_json", exc)
    encoded = _json_line(response).encode("utf-8")
    if len(encoded) > WORKER_MAX_RESPONSE_BYTES:
        encoded = _json_line(
            _protocol_error(None, "response_too_large", "worker response too large")
        ).encode("utf-8")
    sys.stdout.buffer.write(encoded)
    sys.stdout.buffer.flush()
    return 0


def _pump_worker_pipe(stream, limit, state, signal_event):
    try:
        while True:
            chunk = stream.read(65_536)
            if not chunk:
                break
            remaining = limit - len(state["data"])
            if len(chunk) > remaining:
                state["data"].extend(chunk[: max(0, remaining)])
                state["overflow"] = True
                signal_event.set()
                break
            state["data"].extend(chunk)
    except (OSError, ValueError):
        state["error"] = True
        signal_event.set()


def _feed_worker_stdin(stream, payload, state, signal_event):
    try:
        stream.write(payload)
        stream.flush()
    except (BrokenPipeError, OSError, ValueError):
        state["error"] = True
        signal_event.set()
    finally:
        try:
            stream.close()
        except (OSError, ValueError):
            pass


def _kill_worker(process):
    try:
        if os.name == "posix":
            os.killpg(process.pid, signal.SIGKILL)
        else:
            process.kill()
    except PermissionError:
        try:
            process.kill()
        except ProcessLookupError:
            pass
    except ProcessLookupError:
        pass


def _supervise_worker(process, payload):
    event = threading.Event()
    stdout_state = {"data": bytearray(), "overflow": False, "error": False}
    stderr_state = {"data": bytearray(), "overflow": False, "error": False}
    stdin_state = {"error": False}
    threads = [
        threading.Thread(
            target=_pump_worker_pipe,
            args=(
                process.stdout,
                WORKER_MAX_RESPONSE_BYTES,
                stdout_state,
                event,
            ),
            daemon=True,
        ),
        threading.Thread(
            target=_pump_worker_pipe,
            args=(
                process.stderr,
                WORKER_MAX_STDERR_BYTES,
                stderr_state,
                event,
            ),
            daemon=True,
        ),
        threading.Thread(
            target=_feed_worker_stdin,
            args=(process.stdin, payload, stdin_state, event),
            daemon=True,
        ),
    ]
    for thread in threads:
        thread.start()

    deadline = time.monotonic() + WORKER_TIMEOUT_SECONDS
    cause = None
    while True:
        if stdout_state["overflow"]:
            cause = "stdout_overflow"
            break
        if stderr_state["overflow"]:
            cause = "stderr_overflow"
            break
        if stdout_state["error"] or stderr_state["error"] or stdin_state["error"]:
            cause = "pipe_error"
            break
        if process.poll() is not None:
            break
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            cause = "timeout"
            break
        event.wait(min(0.02, remaining))
        event.clear()

    if cause is not None:
        _kill_worker(process)
    try:
        process.wait(timeout=1)
    except subprocess.TimeoutExpired:
        _kill_worker(process)
        process.wait(timeout=1)

    for stream in (process.stdin, process.stdout, process.stderr):
        try:
            stream.close()
        except (OSError, ValueError):
            pass
    for thread in threads:
        thread.join(timeout=1)
    if any(thread.is_alive() for thread in threads):
        cause = cause or "cleanup_error"

    return {
        "returncode": process.returncode,
        "stdout": bytes(stdout_state["data"]),
        "stderr": bytes(stderr_state["data"]),
        "cause": cause,
    }


def _run_jsonl_supervisor():
    while True:
        line = sys.stdin.buffer.readline(WORKER_MAX_LINE_BYTES + 1)
        if line == b"":
            break
        oversized = len(line) > WORKER_MAX_LINE_BYTES
        if oversized and not line.endswith(b"\n"):
            while True:
                remainder = sys.stdin.buffer.readline(65_536)
                if remainder == b"" or remainder.endswith(b"\n"):
                    break
        request_id = None
        request_operation = None
        if not oversized:
            try:
                preview = _strict_json_loads(line.decode("utf-8"))
                if isinstance(preview, dict) and isinstance(
                    preview.get("id"), str
                ) and len(preview["id"].encode("utf-8")) <= 256:
                    request_id = preview["id"]
                    request_operation = preview.get("op")
            except (
                UnicodeDecodeError,
                ValueError,
                json.JSONDecodeError,
            ):
                pass

        if oversized:
            response = _protocol_error(
                request_id, "line_too_large", "request line too large"
            )
        else:
            with tempfile.TemporaryDirectory(prefix="lispy-worker-") as temp:
                try:
                    process = subprocess.Popen(
                        [
                            sys.executable,
                            "-I",
                            "-B",
                            "-X",
                            "utf8",
                            str(Path(__file__).resolve()),
                            "--worker-once",
                        ],
                        stdin=subprocess.PIPE,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        cwd=temp,
                        env=_worker_environment(temp),
                        start_new_session=os.name == "posix",
                    )
                except OSError:
                    response = _protocol_error(
                        request_id,
                        "worker_spawn_failed",
                        "worker process could not start",
                    )
                else:
                    supervised = _supervise_worker(process, line)
                    cause = supervised["cause"]
                    stdout = supervised["stdout"]
                    if cause == "timeout":
                        response = _protocol_error(
                            request_id,
                            "timeout",
                            "worker exceeded wall deadline",
                        )
                    elif cause == "stdout_overflow":
                        response = _protocol_error(
                            request_id,
                            "response_too_large",
                            "worker response too large",
                        )
                    elif cause is not None:
                        response = _protocol_error(
                            request_id,
                            "worker_crash",
                            "worker process failed",
                        )
                    elif supervised["returncode"] != 0:
                        response = _protocol_error(
                            request_id, "worker_crash", "worker process failed"
                        )
                    else:
                        try:
                            response = _strict_json_loads(
                                stdout.decode("utf-8")
                            )
                        except (
                            UnicodeDecodeError,
                            ValueError,
                            json.JSONDecodeError,
                        ) as exc:
                            response = _protocol_error(
                                request_id,
                                "invalid_worker_response",
                                exc,
                            )
                        if not _validate_worker_response_shape(
                            response,
                            request_id,
                            request_operation,
                        ):
                            response = _protocol_error(
                                request_id,
                                "invalid_worker_response",
                                "worker response correlation failed",
                            )
        response_line = _json_line(response)
        if len(response_line.encode("utf-8")) > WORKER_MAX_RESPONSE_BYTES:
            response_line = _json_line(
                _protocol_error(
                    None,
                    "response_too_large",
                    "supervisor response too large",
                )
            )
        sys.stdout.write(response_line)
        sys.stdout.flush()
    return 0


SAMPLE_STATE_FILES = (
    "agents.json",
    "channels.json",
    "memory/zion-philosopher-01.md",
    "stats.json",
    "trending.json",
)


def _sample_state_sha256():
    root = REPO_ROOT / "examples" / "sample-state"
    digest = hashlib.sha256()
    for name in SAMPLE_STATE_FILES:
        relative = name.encode("utf-8")
        content = _resource_bytes(
            f"sample-state/{name}",
            root / name,
        )
        digest.update(len(relative).to_bytes(8, "big"))
        digest.update(relative)
        digest.update(len(content).to_bytes(8, "big"))
        digest.update(content)
    return digest.hexdigest()


def _doctor_worker_request(request):
    try:
        process = subprocess.run(
            [
                sys.executable,
                "-I",
                "-B",
                "-X",
                "utf8",
                str(Path(__file__).resolve()),
                "--jsonl",
            ],
            input=_json_line(request),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="strict",
            env=_worker_environment(),
            timeout=WORKER_TIMEOUT_SECONDS * 2,
        )
    except subprocess.TimeoutExpired:
        raise LispError("doctor worker exceeded wall deadline")
    except OSError as exc:
        raise LispError(f"doctor worker could not start: {exc}")
    if process.returncode != 0:
        raise LispError(
            f"doctor worker exited {process.returncode}: "
            f"{process.stderr.strip()}"
        )
    lines = process.stdout.splitlines()
    if len(lines) != 1:
        raise LispError("doctor worker returned invalid JSONL framing")
    try:
        response = _strict_json_loads(lines[0])
    except (ValueError, json.JSONDecodeError) as exc:
        raise LispError(f"doctor worker returned invalid JSON: {exc}")
    if (
        not isinstance(response, dict)
        or response.get("api") != WORKER_API
        or response.get("id") != request.get("id")
    ):
        raise LispError("doctor worker response correlation failed")
    return response


def _canonical_sha256(value):
    encoded = json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _normalize_sha256_pin(value, name):
    if value is None:
        return None
    if not isinstance(value, str):
        raise InvalidDataError(f"{name} must be text")
    if value.startswith("sha256:"):
        value = value[7:]
    if not re.fullmatch(r"[0-9a-f]{64}", value):
        raise InvalidDataError(
            f"{name} must be lowercase SHA-256"
        )
    return value


def _verify_worker_response_digests(response, request, artifact_sha256):
    if not isinstance(response, dict) or not response.get("ok"):
        raise InvalidDataError("recorded worker response is not successful")
    receipt = response.get("receipt")
    if not isinstance(receipt, dict):
        raise InvalidDataError("recorded worker receipt is missing")
    if receipt.get("artifact_sha256") != artifact_sha256:
        raise InvalidDataError("recorded artifact digest mismatch")
    expected_request = "sha256:" + _canonical_sha256(
        {
            key: value
            for key, value in request.items()
            if key != "id"
        }
    )
    if receipt.get("request_sha256") != expected_request:
        raise InvalidDataError("recorded request digest mismatch")
    outcome_payload = {
        key: value
        for key, value in receipt.items()
        if key != "outcome_sha256"
    }
    expected_outcome = "sha256:" + _canonical_sha256(outcome_payload)
    if receipt.get("outcome_sha256") != expected_outcome:
        raise InvalidDataError("recorded outcome digest mismatch")


def _doctor_request(source_sha256):
    return {
        "api": WORKER_API,
        "id": "doctor-replay",
        "op": "hosted-governor",
        "source_id": "lispy/hosted-doctor@1",
        "expected_source_sha256": source_sha256,
        "inputs": {
            "sol": 1,
            "o2_days": 3.0,
            "h2o_days": 20.0,
            "food_days": 20.0,
            "power_kwh": 200.0,
            "colony_risk_index": 10.0,
        },
        "mutable_outputs": {
            "heating_alloc": 0.25,
            "isru_alloc": 0.40,
            "greenhouse_alloc": 0.35,
            "food_ration": 1.0,
        },
        "contract_id": "mars-barn/governor-controls@1",
        "intent_scope": "doctor-sol-1",
    }


def _doctor_report(manifest, request, first, second, before, after):
    receipt = first.get("receipt") if first.get("ok") else None
    effects = receipt.get("effects", []) if receipt else []
    source = next(
        (
            item
            for item in manifest.get("sources", [])
            if item.get("id") == request["source_id"]
        ),
        None,
    )
    checks = {
        "source_manifest_match": bool(
            source
            and receipt
            and receipt.get("source_id") == source["id"]
            and receipt.get("source_sha256") == source["source_sha256"]
            and request["expected_source_sha256"] == source["source_sha256"]
            and receipt.get("artifact_sha256")
            == manifest.get("artifact_sha256")
        ),
        "receipt_accepted": bool(
            receipt and receipt.get("status") == "accepted"
        ),
        "effects_dry_run": bool(
            len(effects) == 1
            and effects[0].get("mode") == "dry_run"
            and effects[0].get("applied") is False
            and effects[0].get("type") == "rappterbook.post.create"
            and effects[0].get("idempotency_key")
            == effect_idempotency_key(
                receipt["contract_id"],
                receipt["intent_scope"],
                0,
            )
            and effects[0].get("effect_sha256")
            == effect_digest(
                receipt["source_sha256"],
                effects[0]["type"],
                effects[0]["payload"],
            )
        ),
        "state_unchanged": before == after,
        "replay_match": first == second,
    }
    return {
        "api": "lispy.doctor/v1",
        "runtime_version": VERSION,
        "ok": all(checks.values()),
        "checks": checks,
        "state_sha256": before,
        "source_sha256": source["source_sha256"] if source else None,
        "receipt": receipt,
    }


def _print_doctor_report(report, json_output):
    if json_output:
        if report.get("api") == "lispy.doctor/v3":
            _validate_doctor_v3_report(report)
            encoded = json.dumps(
                report,
                allow_nan=False,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            if len((encoded + "\n").encode("utf-8")) > DOCTOR_V3_MAX_REPORT_BYTES:
                raise InvalidDataError(
                    "doctor v3 report exceeds byte limit"
                )
            sys.stdout.write(encoded + "\n")
            return
        print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    else:
        print(f"LisPy doctor: {'PASS' if report['ok'] else 'FAIL'}")
        if "checks" not in report:
            for name, component in report["components"].items():
                if component is not None:
                    print(
                        f"  {'ok' if component['ok'] else 'FAIL'}  "
                        f"{name}"
                    )
            return
        checks = report["checks"]
        if isinstance(checks, dict):
            for name, passed in checks.items():
                print(f"  {'ok' if passed else 'FAIL'}  {name}")
        else:
            for check in checks:
                print(
                    f"  {'ok' if check['ok'] else 'FAIL'}  "
                    f"{check['id']}"
                )


def _collect_replay_evidence():
    before = _sample_state_sha256()
    manifest_response = _doctor_worker_request(
        {"api": WORKER_API, "id": "doctor-manifest", "op": "manifest"}
    )
    if not manifest_response.get("ok"):
        raise LispError("doctor could not read worker manifest")
    manifest = manifest_response["manifest"]
    source = next(
        item
        for item in manifest["sources"]
        if item["id"] == "lispy/hosted-doctor@1"
    )
    request = _doctor_request(source["source_sha256"])
    first = _doctor_worker_request(request)
    second = _doctor_worker_request(request)
    after = _sample_state_sha256()
    return manifest, request, first, second, before, after


def _doctor_check(check_id, ok, *, details=None, error=None):
    return {
        "id": check_id,
        "ok": bool(ok),
        "details": details or {},
        "error": error,
    }


INSTALLED_REQUIRED_PATHS = (
    "lisp.py",
    "effect_executor.py",
    "lisppy/__init__.py",
    "lisppy/__main__.py",
    "lisppy/cli.py",
    "lisppy/contracts.py",
    "lisppy/demo.py",
    "lisppy/effects.py",
    "lisppy/host.py",
    "lisppy/mars.py",
    "lisppy/data/stdlib.lisp",
    "lisppy/data/core-stdlib.lisp",
    "lisppy/data/rappterbook-stdlib.lisp",
    "lisppy/data/mars/governor-contract.json",
    "lisppy/data/mars/governor-vectors.json",
    "lisppy/data/registered/hosted-governor.lisp",
    "lisppy/data/registered/mars-colony-governor.lisp",
    "lisppy/data/sample-state/agents.json",
    "lisppy/data/sample-state/channels.json",
    "lisppy/data/sample-state/stats.json",
    "lisppy/data/sample-state/trending.json",
    "lisppy/data/sample-state/memory/zion-philosopher-01.md",
    "lisppy/data/contracts/lispy-core@1/README.md",
    "lisppy/data/contracts/lispy-core@1/CONFORMANCE.md",
    "lisppy/data/contracts/lispy-core@1/conformance.json",
    "lisppy/data/contracts/lispy-core@1/profile.json",
    "lisppy/data/contracts/lispy-core@1/stdlib.lisp",
)
BUILD_INPUT_PATHS = (
    "MANIFEST.in",
    "README.md",
    "pyproject.toml",
    "setup.cfg",
    "setup.py",
)


def _verify_distribution_record(distribution):
    record_text = distribution.read_text("RECORD")
    if record_text is None:
        raise InvalidDataError("installed distribution RECORD is missing")
    if len(record_text.encode("utf-8")) > WORKER_MAX_RESPONSE_BYTES:
        raise InvalidDataError("installed distribution RECORD is oversized")
    rows = {}
    for row in csv.reader(record_text.splitlines()):
        if len(row) != 3 or row[0] in rows:
            raise InvalidDataError("installed distribution RECORD is invalid")
        rows[row[0]] = (row[1], row[2])
    _validate_distribution_record_paths(rows)

    artifacts = []
    for relative in INSTALLED_REQUIRED_PATHS:
        if relative not in rows:
            raise InvalidDataError(f"RECORD missing required path: {relative}")
        hash_field, size_field = rows[relative]
        if (
            not size_field.isdigit()
            or str(int(size_field)) != size_field
        ):
            raise InvalidDataError(f"RECORD size is invalid: {relative}")
        if not hash_field.startswith("sha256="):
            raise InvalidDataError(f"RECORD missing SHA-256: {relative}")
        path = Path(distribution.locate_file(relative)).resolve()
        content = path.read_bytes()
        expected_hash = hash_field.split("=", 1)[1]
        actual_hash = base64.urlsafe_b64encode(
            hashlib.sha256(content).digest()
        ).decode("ascii").rstrip("=")
        if actual_hash != expected_hash or int(size_field) != len(content):
            raise InvalidDataError(f"RECORD hash mismatch: {relative}")
        artifacts.append(
            {
                "path": relative,
                "size": len(content),
                "sha256": hashlib.sha256(content).hexdigest(),
            }
        )
    for relative, (hash_field, size_field) in rows.items():
        if relative in INSTALLED_REQUIRED_PATHS:
            continue
        if not hash_field and not size_field and (
            relative.endswith(".pyc")
            or relative.endswith(".dist-info/RECORD")
        ):
            continue
        _verify_record_content(
            distribution,
            relative,
            hash_field,
            size_field,
        )
    return sorted(artifacts, key=lambda item: item["path"])


def _verify_record_content(distribution, relative, hash_field, size_field):
    if (
        not size_field.isdigit()
        or str(int(size_field)) != size_field
        or not hash_field.startswith("sha256=")
    ):
        raise InvalidDataError(f"RECORD metadata is invalid: {relative}")
    path = Path(distribution.locate_file(relative))
    if path.is_symlink() or not path.is_file():
        raise InvalidDataError(f"RECORD path is not a regular file: {relative}")
    content = path.read_bytes()
    actual_hash = base64.urlsafe_b64encode(
        hashlib.sha256(content).digest()
    ).decode("ascii").rstrip("=")
    if (
        actual_hash != hash_field.split("=", 1)[1]
        or int(size_field) != len(content)
    ):
        raise InvalidDataError(f"RECORD hash mismatch: {relative}")


def _validate_distribution_record_paths(rows):
    required = set(INSTALLED_REQUIRED_PATHS)
    metadata_pattern = re.compile(
        r"^rappterbook_lispy_runtime-[^/]+\.dist-info/"
        r"(?:INSTALLER|LICENSE|METADATA|RECORD|REQUESTED|WHEEL|direct_url\.json|"
        r"entry_points\.txt|top_level\.txt|licenses/LICENSE)$"
    )
    pyc_patterns = []
    for relative in INSTALLED_REQUIRED_PATHS:
        if not relative.endswith(".py"):
            continue
        module = relative[:-3]
        directory, _, name = module.rpartition("/")
        canonical = (
            f"{directory}/__pycache__/{name}"
            if directory
            else f"__pycache__/{name}"
        )
        cached = module
        pyc_patterns.extend(
            [
                re.compile(
                    r"^" + re.escape(canonical) + r"\.cpython-[0-9]+\.pyc$"
                ),
                re.compile(
                    r"^(?:\.\./)+.+/site-packages/"
                    + re.escape(cached)
                    + r"\.cpython-[0-9]+\.pyc$"
                ),
            ]
        )
    for path, (hash_field, size_field) in rows.items():
        if path in required or metadata_pattern.fullmatch(path):
            continue
        if path == "../../../bin/lispy":
            continue
        if (
            not hash_field
            and not size_field
            and any(pattern.search(path) for pattern in pyc_patterns)
        ):
            continue
        raise InvalidDataError(
            f"RECORD contains unexpected installed path: {path}"
        )


def _installed_doctor_profile():
    check_ids = (
        "distribution.present",
        "distribution.metadata",
        "distribution.version",
        "distribution.origin",
        "distribution.entrypoint",
        "distribution.record",
        "distribution.resources",
        "distribution.inventory",
    )
    checks = []
    artifacts = []
    try:
        distribution = importlib.metadata.distribution(DISTRIBUTION_NAME)
    except importlib.metadata.PackageNotFoundError:
        for check_id in check_ids:
            checks.append(
                _doctor_check(
                    check_id,
                    False,
                    error={
                        "category": "package",
                        "code": "distribution_not_installed",
                    },
                )
            )
    else:
        checks.append(_doctor_check("distribution.present", True))
        metadata_name = distribution.metadata.get("Name", "")
        normalized_name = re.sub(r"[-_.]+", "-", metadata_name).lower()
        checks.append(
            _doctor_check(
                "distribution.metadata",
                normalized_name == DISTRIBUTION_NAME,
                details={"name": metadata_name},
            )
        )
        checks.append(
            _doctor_check(
                "distribution.version",
                distribution.version == VERSION,
                details={
                    "expected": VERSION,
                    "actual": distribution.version,
                },
            )
        )
        try:
            import effect_executor as executor_module
            import lisppy as package_module

            origins = {
                "lisp.py": Path(__file__).resolve(),
                "effect_executor.py": Path(executor_module.__file__).resolve(),
                "lisppy/__init__.py": Path(package_module.__file__).resolve(),
            }
            origin_ok = all(
                path.samefile(distribution.locate_file(relative))
                for relative, path in origins.items()
            )
        except (AttributeError, ImportError, OSError, TypeError):
            origin_ok = False
        checks.append(_doctor_check("distribution.origin", origin_ok))
        console_entries = [
            item
            for item in distribution.entry_points
            if item.group == "console_scripts" and item.name == "lispy"
        ]
        checks.append(
            _doctor_check(
                "distribution.entrypoint",
                len(console_entries) == 1
                and console_entries[0].value == "lisppy.cli:main",
            )
        )
        try:
            artifacts = _verify_distribution_record(distribution)
        except (csv.Error, InvalidDataError, OSError, ValueError) as exc:
            checks.append(
                _doctor_check(
                    "distribution.record",
                    False,
                    error={
                        "category": "package",
                        "code": "record_verification_failed",
                        "message": str(exc),
                    },
                )
            )
            checks.append(
                _doctor_check("distribution.resources", False)
            )
            checks.append(
                _doctor_check("distribution.inventory", False)
            )
        else:
            checks.append(_doctor_check("distribution.record", True))
            checks.append(
                _doctor_check(
                    "distribution.resources",
                    len(artifacts) == len(INSTALLED_REQUIRED_PATHS),
                )
            )
            checks.append(
                _doctor_check(
                    "distribution.inventory",
                    True,
                    details={
                        "sha256": _canonical_sha256(
                            {
                                "api": "lispy.inventory/v1",
                                "distribution": DISTRIBUTION_NAME,
                                "version": VERSION,
                                "files": artifacts,
                            }
                        )
                    },
                )
            )
    order = {check_id: index for index, check_id in enumerate(check_ids)}
    checks.sort(key=lambda check: order[check["id"]])
    return checks, sorted(artifacts, key=lambda item: item["path"])


def _effects_doctor_profile(evidence=None):
    from effect_executor import (
        EffectAdapterRegistry,
        InMemoryIdempotencyStore,
        execute_effects_batch,
        proposal_sha256,
    )

    manifest, request, first, second, before, after = (
        evidence or _collect_replay_evidence()
    )
    replay_report = _doctor_report(
        manifest, request, first, second, before, after
    )
    receipt = first.get("receipt")
    calls = []
    registry = EffectAdapterRegistry()

    def validate(payload):
        return [] if set(payload) == {"channel", "title", "body"} else ["shape"]

    def execute(payload, context):
        calls.append({"payload": payload, "context": context})
        return {"recorded": True}

    registry.register(
        "rappterbook.post.create",
        adapter_id="doctor-recording@1",
        validate=validate,
        execute=execute,
    )
    registry.freeze()
    pins = {
        "source_id": receipt["source_id"],
        "source_sha256": receipt["source_sha256"],
        "contract_id": receipt["contract_id"],
        "intent_scope": receipt["intent_scope"],
        "proposal_sha256": proposal_sha256(receipt),
        "namespace": "doctor",
        "adapter_ids": {
            "rappterbook.post.create": "doctor-recording@1",
        },
    }
    store = InMemoryIdempotencyStore()
    options = {
        "expected": pins,
        "registry": registry,
        "store": store,
        "namespace": "doctor",
    }
    first_execution = execute_effects_batch(
        receipt,
        execution_id="doctor-effects-first",
        **options,
    )
    second_execution = execute_effects_batch(
        receipt,
        execution_id="doctor-effects-second",
        **options,
    )
    checks = [
        _doctor_check("effects.replay-prerequisite", replay_report["ok"]),
        _doctor_check(
            "effects.first-application",
            [item["status"] for item in first_execution["effects"]]
            == ["applied"],
        ),
        _doctor_check(
            "effects.idempotent-replay",
            [item["status"] for item in second_execution["effects"]]
            == ["duplicate_applied"]
            and len(calls) == 1,
        ),
    ]
    return checks


def _doctor_v2(profile):
    checks = []
    artifacts = []
    if profile in ("installed@1", "release@1"):
        installed_checks, artifacts = _installed_doctor_profile()
        checks.extend(installed_checks)
    if profile in ("effects@1", "release@1"):
        checks.extend(_effects_doctor_profile())
    return {
        "api": "lispy.doctor/v2",
        "profile": profile,
        "runtime_version": VERSION,
        "ok": all(check["ok"] for check in checks),
        "checks": checks,
        "artifacts": artifacts,
    }


def _source_inventory():
    files = []
    for relative in INSTALLED_REQUIRED_PATHS:
        path = (REPO_ROOT / relative).resolve()
        try:
            path.relative_to(REPO_ROOT.resolve())
        except ValueError:
            raise InvalidDataError("source inventory path escapes root")
        if relative.startswith("lisppy/data/"):
            content = _resource_bytes(
                relative[len("lisppy/data/"):],
                path,
            )
        else:
            content = path.read_bytes()
        files.append(
            {
                "path": relative,
                "size": len(content),
                "sha256": hashlib.sha256(content).hexdigest(),
            }
        )
    return sorted(files, key=lambda item: item["path"])


def build_source_sha256():
    files = []
    for relative in BUILD_INPUT_PATHS:
        content = (REPO_ROOT / relative).read_bytes()
        files.append(
            {
                "path": relative,
                "size": len(content),
                "sha256": hashlib.sha256(content).hexdigest(),
            }
        )
    return _canonical_sha256(
        {
            "api": "lispy.build-source/v1",
            "files": files,
        }
    )


def _inventory_payload(files):
    payload = {
        "api": "lispy.inventory/v1",
        "distribution": DISTRIBUTION_NAME,
        "version": VERSION,
        "language_profile": LANGUAGE_PROFILE,
        "files": files,
    }
    return {**payload, "sha256": _canonical_sha256(payload)}


def _v3_component(component_id, checks, evidence):
    normalized = []
    for check in checks:
        ok = bool(check["ok"])
        error = check.get("error")
        if ok:
            error = None
        elif error is None:
            error = {
                "category": "doctor",
                "code": "check_failed",
            }
        normalized.append(
            {
                "id": check["id"],
                "ok": ok,
                "error": error,
            }
        )
    return {
        "id": component_id,
        "ok": all(check["ok"] for check in normalized),
        "checks": normalized,
        "evidence": evidence,
    }


def _v3_failed_component(component_id, code):
    return {
        "id": component_id,
        "ok": False,
        "checks": [
            {
                "id": f"{component_id}.execution",
                "ok": False,
                "error": {
                    "category": "doctor",
                    "code": code,
                },
            }
        ],
        "evidence": {},
    }


DOCTOR_V3_CHECK_CATALOGS = {
    "inventory": {
        "source": (
            "inventory.source-files",
            "inventory.external-pin",
        ),
        "installed": (
            "distribution.present",
            "distribution.metadata",
            "distribution.version",
            "distribution.origin",
            "distribution.entrypoint",
            "distribution.record",
            "distribution.resources",
            "distribution.inventory",
            "inventory.external-pin",
        ),
    },
    "replay": (
        "source_manifest_match",
        "receipt_accepted",
        "effects_dry_run",
        "state_unchanged",
        "replay_match",
    ),
    "effects": (
        "effects.replay-prerequisite",
        "effects.first-application",
        "effects.idempotent-replay",
    ),
}


def _validate_doctor_v3_error(error, *, required):
    if error is None:
        if required:
            raise InvalidDataError("doctor v3 failure is missing an error")
        return
    if not isinstance(error, dict):
        raise InvalidDataError("invalid doctor v3 error")
    if not {"category", "code"} <= set(error) <= {
        "category",
        "code",
        "message",
    }:
        raise InvalidDataError("invalid doctor v3 error fields")
    if (
        not isinstance(error["category"], str)
        or not error["category"]
        or not isinstance(error["code"], str)
        or not error["code"]
        or (
            "message" in error
            and (
                not isinstance(error["message"], str)
                or not error["message"]
            )
        )
    ):
        raise InvalidDataError("invalid doctor v3 error value")


def _validate_doctor_v3_sha256(value, field):
    if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None:
        raise InvalidDataError(f"invalid doctor v3 {field}")


def _validate_doctor_v3_report(report):
    required = {
        "api",
        "profile",
        "mode",
        "runtime",
        "pins",
        "ok",
        "components",
        "artifacts",
        "error",
    }
    if not isinstance(report, dict) or set(report) != required:
        raise InvalidDataError("invalid doctor v3 report fields")
    if report["api"] != "lispy.doctor/v3" or type(report["ok"]) is not bool:
        raise InvalidDataError("invalid doctor v3 report identity")
    matrix = {
        "inventory@1": {"inventory"},
        "replay@2": {"inventory", "replay"},
        "effects@2": {"inventory", "effects"},
        "release@2": {"inventory", "replay", "effects"},
    }
    if (
        not isinstance(report["profile"], str)
        or not isinstance(report["mode"], str)
        or report["profile"] not in matrix
        or report["mode"] not in ("source", "installed")
    ):
        raise InvalidDataError("invalid doctor v3 profile or mode")
    runtime = report["runtime"]
    if (
        not isinstance(runtime, dict)
        or set(runtime)
        != {"distribution", "version", "language_profile"}
        or runtime["distribution"] != DISTRIBUTION_NAME
        or runtime["version"] != VERSION
        or runtime["language_profile"] != LANGUAGE_PROFILE
    ):
        raise InvalidDataError("invalid doctor v3 runtime")
    pins = report["pins"]
    if not isinstance(pins, dict) or set(pins) != {
        "inventory_sha256",
        "actual_inventory_sha256",
    }:
        raise InvalidDataError("invalid doctor v3 pins")
    for value in pins.values():
        if value is not None:
            _validate_doctor_v3_sha256(value, "pin")
    components = report["components"]
    if not isinstance(components, dict) or set(components) != {
        "inventory",
        "replay",
        "effects",
    }:
        raise InvalidDataError("invalid doctor v3 components")
    selected = matrix[report["profile"]]
    for name, component in components.items():
        if name not in selected:
            if component is not None:
                raise InvalidDataError("unexpected doctor v3 component")
            continue
        if (
            not isinstance(component, dict)
            or set(component) != {"id", "ok", "checks", "evidence"}
            or component["id"] != name
            or type(component["ok"]) is not bool
            or not isinstance(component["checks"], list)
            or not isinstance(component["evidence"], dict)
        ):
            raise InvalidDataError("invalid doctor v3 component shape")
        seen = set()
        check_ids = []
        for check in component["checks"]:
            if (
                not isinstance(check, dict)
                or set(check) != {"id", "ok", "error"}
                or not isinstance(check["id"], str)
                or not check["id"]
                or check["id"] in seen
                or type(check["ok"]) is not bool
            ):
                raise InvalidDataError("invalid doctor v3 check")
            seen.add(check["id"])
            check_ids.append(check["id"])
            _validate_doctor_v3_error(
                check["error"],
                required=not check["ok"],
            )
            if check["ok"] and check["error"] is not None:
                raise InvalidDataError(
                    "successful doctor v3 check has an error"
                )
        execution_catalog = [f"{name}.execution"]
        expected_catalog = (
            list(DOCTOR_V3_CHECK_CATALOGS["inventory"][report["mode"]])
            if name == "inventory"
            else list(DOCTOR_V3_CHECK_CATALOGS[name])
        )
        if check_ids not in (execution_catalog, expected_catalog):
            raise InvalidDataError("invalid doctor v3 check catalog")
        if check_ids == execution_catalog and (
            component["ok"]
            or component["checks"][0]["ok"]
            or component["evidence"] != {}
        ):
            raise InvalidDataError(
                "invalid doctor v3 component execution failure"
            )
        if component["ok"] != all(
            check["ok"] for check in component["checks"]
        ):
            raise InvalidDataError("doctor v3 component status mismatch")
    aggregate = all(
        component is None or component["ok"]
        for component in components.values()
    )
    if report["ok"] != aggregate:
        raise InvalidDataError("doctor v3 aggregate status mismatch")
    if (report["error"] is None) != report["ok"]:
        raise InvalidDataError("doctor v3 error status mismatch")
    if report["ok"]:
        _validate_doctor_v3_error(report["error"], required=False)
    elif report["error"] != {
        "category": "doctor",
        "code": "component_failure",
    }:
        raise InvalidDataError("invalid doctor v3 top-level error")
    artifacts = report["artifacts"]
    if not isinstance(artifacts, list):
        raise InvalidDataError("invalid doctor v3 artifacts")
    paths = []
    for artifact in artifacts:
        if (
            not isinstance(artifact, dict)
            or set(artifact) != {"path", "size", "sha256"}
            or not isinstance(artifact["path"], str)
            or isinstance(artifact["size"], bool)
            or not isinstance(artifact["size"], int)
            or artifact["size"] < 0
        ):
            raise InvalidDataError("invalid doctor v3 artifact")
        _validate_doctor_v3_sha256(
            artifact["sha256"],
            "artifact SHA-256",
        )
        paths.append(artifact["path"])
    if paths != sorted(paths) or len(paths) != len(set(paths)):
        raise InvalidDataError("doctor v3 artifacts are not canonical")
    inventory_component = components["inventory"]
    inventory_evidence = inventory_component["evidence"]
    if inventory_evidence:
        if set(inventory_evidence) != {"inventory"}:
            raise InvalidDataError("invalid doctor v3 inventory evidence")
        inventory = inventory_evidence["inventory"]
        if (
            not isinstance(inventory, dict)
            or set(inventory)
            != {
                "api",
                "distribution",
                "version",
                "language_profile",
                "files",
                "sha256",
            }
            or inventory["api"] != "lispy.inventory/v1"
            or inventory["distribution"] != DISTRIBUTION_NAME
            or inventory["version"] != VERSION
            or inventory["language_profile"] != LANGUAGE_PROFILE
            or inventory["files"] != artifacts
        ):
            raise InvalidDataError("doctor v3 inventory/artifact mismatch")
        payload = {
            key: value for key, value in inventory.items()
            if key != "sha256"
        }
        if inventory.get("sha256") != _canonical_sha256(payload):
            raise InvalidDataError("doctor v3 inventory digest mismatch")
        actual = report["pins"]["actual_inventory_sha256"]
        if actual != inventory["sha256"]:
            raise InvalidDataError("doctor v3 pin/inventory mismatch")
        external_pin_check = inventory_component["checks"][-1]
        expected_pin = report["pins"]["inventory_sha256"]
        pin_matches = (
            expected_pin is None
            or expected_pin == inventory["sha256"]
        )
        if external_pin_check["id"] != "inventory.external-pin":
            raise InvalidDataError("missing doctor v3 inventory pin check")
        if external_pin_check["ok"] != pin_matches:
            raise InvalidDataError("doctor v3 inventory pin status mismatch")
        if (
            not pin_matches
            and (
                external_pin_check["error"] is None
                or external_pin_check["error"].get("code")
                != "inventory_pin_mismatch"
            )
        ):
            raise InvalidDataError("doctor v3 inventory pin error mismatch")
    elif (
        artifacts
        or report["pins"]["actual_inventory_sha256"] is not None
        or [check["id"] for check in inventory_component["checks"]]
        != ["inventory.execution"]
    ):
        raise InvalidDataError("invalid failed doctor v3 inventory")

    replay_component = components["replay"]
    if replay_component is not None:
        replay_ids = [check["id"] for check in replay_component["checks"]]
        if replay_ids == list(DOCTOR_V3_CHECK_CATALOGS["replay"]):
            if set(replay_component["evidence"]) != {
                "artifact_sha256",
                "source_sha256",
                "state_before_sha256",
                "state_after_sha256",
                "first_response_sha256",
                "second_response_sha256",
            }:
                raise InvalidDataError("invalid doctor v3 replay evidence")
            for value in replay_component["evidence"].values():
                _validate_doctor_v3_sha256(
                    value,
                    "replay evidence SHA-256",
                )
            replay_checks = {
                check["id"]: check
                for check in replay_component["checks"]
            }
            evidence = replay_component["evidence"]
            if replay_checks["state_unchanged"]["ok"] != (
                evidence["state_before_sha256"]
                == evidence["state_after_sha256"]
            ):
                raise InvalidDataError(
                    "doctor v3 state evidence mismatch"
                )
            if replay_checks["replay_match"]["ok"] != (
                evidence["first_response_sha256"]
                == evidence["second_response_sha256"]
            ):
                raise InvalidDataError(
                    "doctor v3 replay evidence mismatch"
                )

    effects_component = components["effects"]
    if effects_component is not None:
        effects_ids = [check["id"] for check in effects_component["checks"]]
        if effects_ids == list(DOCTOR_V3_CHECK_CATALOGS["effects"]):
            if set(effects_component["evidence"]) != {"checks_sha256"}:
                raise InvalidDataError("invalid doctor v3 effects evidence")
            _validate_doctor_v3_sha256(
                effects_component["evidence"]["checks_sha256"],
                "effects evidence SHA-256",
            )
            if (
                effects_component["evidence"]["checks_sha256"]
                != _canonical_sha256(effects_component["checks"])
            ):
                raise InvalidDataError(
                    "doctor v3 effects evidence digest mismatch"
                )
    encoded = json.dumps(
        report,
        allow_nan=False,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    if len(encoded) + 1 > DOCTOR_V3_MAX_REPORT_BYTES:
        raise InvalidDataError("doctor v3 report exceeds byte limit")
    return report


def _doctor_v3(profile, mode, expected_inventory):
    if mode not in ("source", "installed"):
        raise LispError(
            "doctor v3 requires --doctor-mode source or installed"
        )
    expected_pin = _normalize_sha256_pin(
        expected_inventory, "expected inventory pin"
    )
    components = {
        "inventory": None,
        "replay": None,
        "effects": None,
    }
    files = []
    replay_evidence = None
    try:
        if mode == "source":
            files = _source_inventory()
            inventory_checks = [
                _doctor_check("inventory.source-files", True)
            ]
        else:
            inventory_checks, files = _installed_doctor_profile()
        inventory = _inventory_payload(files)
        pin_ok = (
            expected_pin is None
            or expected_pin == inventory["sha256"]
        )
        inventory_checks.append(
            _doctor_check(
                "inventory.external-pin",
                pin_ok,
                error=(
                    None
                    if pin_ok
                    else {
                        "category": "package",
                        "code": "inventory_pin_mismatch",
                    }
                ),
            )
        )
        components["inventory"] = _v3_component(
            "inventory",
            inventory_checks,
            {"inventory": inventory},
        )
    except Exception:
        inventory = None
        files = []
        components["inventory"] = _v3_failed_component(
            "inventory", "inventory_component_failed"
        )
    if profile in ("replay@2", "release@2"):
        try:
            replay_evidence = _collect_replay_evidence()
            manifest, request, first, second, before, after = replay_evidence
            replay = _doctor_report(
                manifest, request, first, second, before, after
            )
            components["replay"] = _v3_component(
                "replay",
                [
                    _doctor_check(name, passed)
                    for name, passed in replay["checks"].items()
                ],
                {
                    "artifact_sha256": manifest["artifact_sha256"],
                    "source_sha256": replay["source_sha256"],
                    "state_before_sha256": before,
                    "state_after_sha256": after,
                    "first_response_sha256": _canonical_sha256(first),
                    "second_response_sha256": _canonical_sha256(second),
                },
            )
        except Exception:
            components["replay"] = _v3_failed_component(
                "replay", "replay_component_failed"
            )
    if profile in ("effects@2", "release@2"):
        try:
            effect_checks = _effects_doctor_profile(replay_evidence)
            effect_component = _v3_component(
                "effects",
                effect_checks,
                {},
            )
            effect_component["evidence"] = {
                "checks_sha256": _canonical_sha256(
                    effect_component["checks"]
                ),
            }
            components["effects"] = effect_component
        except Exception:
            components["effects"] = _v3_failed_component(
                "effects", "effects_component_failed"
            )
    report = {
        "api": "lispy.doctor/v3",
        "profile": profile,
        "mode": mode,
        "runtime": {
            "distribution": DISTRIBUTION_NAME,
            "version": VERSION,
            "language_profile": LANGUAGE_PROFILE,
        },
        "pins": {
            "inventory_sha256": expected_pin,
            "actual_inventory_sha256": (
                inventory["sha256"] if inventory is not None else None
            ),
        },
        "ok": all(
            component is None or component["ok"]
            for component in components.values()
        ),
        "components": components,
        "artifacts": files,
        "error": (
            None
            if all(
                component is None or component["ok"]
                for component in components.values()
            )
            else {
                "category": "doctor",
                "code": "component_failure",
            }
        ),
    }
    return _validate_doctor_v3_report(report)


def _run_doctor(
    *,
    profile="replay@1",
    json_output=False,
    export_path=None,
    mode=None,
    expected_inventory=None,
):
    if profile in ("inventory@1", "replay@2", "effects@2", "release@2"):
        if export_path is not None:
            raise LispError(
                "--export-replay is supported only by replay@1"
            )
        report = _doctor_v3(profile, mode, expected_inventory)
        _print_doctor_report(report, json_output)
        return 0 if report["ok"] else 1
    if profile != "replay@1":
        if export_path is not None:
            raise LispError(
                "--export-replay is supported only by replay@1"
            )
        report = _doctor_v2(profile)
        _print_doctor_report(report, json_output)
        return 0 if report["ok"] else 1

    manifest, request, first, second, before, after = (
        _collect_replay_evidence()
    )
    report = _doctor_report(
        manifest, request, first, second, before, after
    )
    if export_path is not None:
        payload = {
            "api": "lispy.replay-bundle/v1",
            "runtime_version": VERSION,
            "manifest": manifest,
            "request": request,
            "state_sha256": before,
            "first_response": first,
            "second_response": second,
        }
        bundle = {
            **payload,
            "bundle_sha256": _canonical_sha256(payload),
        }
        destination = Path(export_path)
        temporary = destination.with_name(destination.name + ".tmp")
        temporary.write_text(
            json.dumps(
                bundle,
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        temporary.replace(destination)
        report["replay_bundle"] = str(destination)
        report["bundle_sha256"] = bundle["bundle_sha256"]
    _print_doctor_report(report, json_output)
    return 0 if report["ok"] else 1


def _run_replay(
    path,
    *,
    json_output=False,
    expected_bundle=None,
    expected_artifact=None,
):
    replay_path = Path(path)
    with open(replay_path, "rb") as replay_file:
        raw = replay_file.read(WORKER_MAX_RESPONSE_BYTES + 1)
    if len(raw) > WORKER_MAX_RESPONSE_BYTES:
        raise InvalidDataError("replay bundle exceeds size limit")
    try:
        bundle = _strict_json_loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, json.JSONDecodeError) as exc:
        raise InvalidDataError(f"invalid replay bundle: {exc}")
    if not isinstance(bundle, dict) or bundle.get("api") != "lispy.replay-bundle/v1":
        raise InvalidDataError("unsupported replay bundle")
    required_fields = {
        "api",
        "runtime_version",
        "manifest",
        "request",
        "state_sha256",
        "first_response",
        "second_response",
        "bundle_sha256",
    }
    if set(bundle) != required_fields:
        raise InvalidDataError("invalid replay bundle fields")
    digest = bundle.get("bundle_sha256")
    payload = {
        key: value
        for key, value in bundle.items()
        if key != "bundle_sha256"
    }
    if digest != _canonical_sha256(payload):
        raise InvalidDataError("replay bundle digest mismatch")
    expected_bundle = _normalize_sha256_pin(
        expected_bundle, "expected bundle pin"
    )
    if expected_bundle is not None and digest != expected_bundle:
        raise InvalidDataError("external bundle pin mismatch")
    request = bundle["request"]
    if (
        not isinstance(request, dict)
        or request.get("api") != WORKER_API
        or request.get("op") != "hosted-governor"
        or not isinstance(request.get("source_id"), str)
        or not isinstance(request.get("expected_source_sha256"), str)
    ):
        raise InvalidDataError("invalid replay request")
    if not isinstance(bundle["manifest"], dict):
        raise InvalidDataError("invalid replay manifest")
    bundled_artifact = bundle["manifest"].get("artifact_sha256")
    expected_artifact = _normalize_sha256_pin(
        expected_artifact, "expected artifact pin"
    )
    if (
        expected_artifact is not None
        and bundled_artifact != expected_artifact
    ):
        raise InvalidDataError("external artifact pin mismatch")
    _verify_worker_response_digests(
        bundle["first_response"],
        request,
        bundled_artifact,
    )
    _verify_worker_response_digests(
        bundle["second_response"],
        request,
        bundled_artifact,
    )

    current_manifest_response = _doctor_worker_request(
        {"api": WORKER_API, "id": "doctor-manifest", "op": "manifest"}
    )
    current_manifest = current_manifest_response.get("manifest")
    before = _sample_state_sha256()
    first = _doctor_worker_request(request)
    second = _doctor_worker_request(request)
    after = _sample_state_sha256()
    semantic_report = _doctor_report(
        current_manifest,
        request,
        first,
        second,
        before,
        after,
    )
    checks = {
        "bundle_digest": True,
        "runtime_match": bundle["runtime_version"] == VERSION,
        "manifest_match": current_manifest == bundle["manifest"],
        "artifact_pin": (
            expected_artifact is None
            or current_manifest.get("artifact_sha256") == expected_artifact
        ),
        "bundle_pin": expected_bundle is None or digest == expected_bundle,
        "state_baseline_match": before == bundle["state_sha256"],
        "first_response_match": first == bundle["first_response"],
        "second_response_match": second == bundle["second_response"],
        "state_unchanged": before == after,
        "doctor_semantics": semantic_report["ok"],
    }
    report = {
        "api": "lispy.replay/v1",
        "runtime_version": VERSION,
        "ok": all(checks.values()),
        "checks": checks,
        "bundle_sha256": digest,
    }
    _print_doctor_report(report, json_output)
    return 0 if report["ok"] else 1


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

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
                        "error": {
                            "category": "usage",
                            "code": "invalid_arguments",
                            "message": message,
                        },
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                )
            )
            raise SystemExit(2)
        super().error(message)


def _argument_parser() -> argparse.ArgumentParser:
    parser = LispyArgumentParser(
        prog="lispy",
        description="A zero-dependency Lisp interpreter for agent orchestration.",
        allow_abbrev=False,
    )
    parser.add_argument("script", nargs="?", help="Lisp source file to execute")
    parser.add_argument("-e", "--eval", dest="expression", help="evaluate source")
    parser.add_argument(
        "--print-result",
        action="store_true",
        help="print the final value returned by a script",
    )
    parser.add_argument(
        "--trusted",
        action="store_true",
        help="enable host filesystem and Python process capabilities",
    )
    parser.add_argument(
        "--no-stdlib",
        action="store_true",
        help="start without loading stdlib.lisp",
    )
    parser.add_argument(
        "--state-dir",
        metavar="PATH",
        help="read-only root for rb-state and related bindings",
    )
    parser.add_argument(
        "--max-steps",
        type=_positive_int,
        default=None,
        help="maximum evaluator steps per run (default: 100000)",
    )
    parser.add_argument(
        "--max-call-depth",
        type=_positive_int,
        default=None,
        help="maximum LisPy call depth per run (default: 128)",
    )
    parser.add_argument(
        "--max-reader-depth",
        type=_positive_int,
        default=None,
        help="maximum nested reader depth (default: 256)",
    )
    parser.add_argument(
        "--max-source-bytes",
        type=_positive_int,
        default=None,
        help="maximum UTF-8 source size (default: 1048576)",
    )
    parser.add_argument(
        "--max-collection-items",
        type=_positive_int,
        default=None,
        help="maximum items in bounded collection operations (default: 100000)",
    )
    parser.add_argument(
        "--max-output-bytes",
        type=_positive_int,
        default=None,
        help="maximum captured output bytes for hosted runs (default: 1048576)",
    )
    parser.add_argument(
        "--unlimited",
        action="store_true",
        help="disable in-process execution limits (does not grant capabilities)",
    )
    parser.add_argument(
        "--jsonl",
        action="store_true",
        help="run the supervised lispy.worker/v1 JSONL host adapter",
    )
    parser.add_argument(
        "--doctor",
        nargs="?",
        const="replay@1",
        choices=(
            "replay@1",
            "installed@1",
            "effects@1",
            "release@1",
            "inventory@1",
            "replay@2",
            "effects@2",
            "release@2",
        ),
        help="run a named deterministic diagnostic profile",
    )
    parser.add_argument(
        "--doctor-mode",
        choices=("source", "installed"),
        help="resource mode for doctor v3 profiles",
    )
    parser.add_argument(
        "--expect-inventory",
        metavar="SHA256",
        help="require an external doctor inventory SHA-256 pin",
    )
    parser.add_argument(
        "--export-replay",
        metavar="PATH",
        help="write a lispy.replay-bundle/v1 during --doctor",
    )
    parser.add_argument(
        "--replay",
        metavar="PATH",
        help="verify and reproduce a replay bundle",
    )
    parser.add_argument(
        "--expect-bundle",
        metavar="SHA256",
        help="require an external replay-bundle SHA-256 pin",
    )
    parser.add_argument(
        "--expect-artifact",
        metavar="SHA256",
        help="require an external worker-artifact SHA-256 pin",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        dest="json_output",
        help="emit --doctor output as JSON",
    )
    parser.add_argument(
        "--worker-once",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"LisPy {VERSION} ({LANGUAGE_PROFILE})",
    )
    return parser


EXECUTION_OPTION_FLAGS = {
    "print_result": "--print-result",
    "trusted": "--trusted",
    "no_stdlib": "--no-stdlib",
    "state_dir": "--state-dir",
    "max_steps": "--max-steps",
    "max_call_depth": "--max-call-depth",
    "max_reader_depth": "--max-reader-depth",
    "max_source_bytes": "--max-source-bytes",
    "max_collection_items": "--max-collection-items",
    "max_output_bytes": "--max-output-bytes",
    "unlimited": "--unlimited",
}


def _reject_present_options(args, parser, mode, options):
    present = []
    for name, flag in options.items():
        value = getattr(args, name)
        if value is not None and value is not False:
            present.append(flag)
    if present:
        parser.error(f"{mode} does not accept: {', '.join(sorted(present))}")


def _require_nonempty_option(parser, value, flag):
    if value is not None and value == "":
        parser.error(f"{flag} requires a non-empty value")


def _dispatch_special_mode(args, parser):
    if args.worker_once:
        _reject_present_options(
            args,
            parser,
            "--worker-once",
            {
                **EXECUTION_OPTION_FLAGS,
                "script": "script",
                "expression": "--eval",
                "jsonl": "--jsonl",
                "doctor": "--doctor",
                "doctor_mode": "--doctor-mode",
                "expect_inventory": "--expect-inventory",
                "export_replay": "--export-replay",
                "replay": "--replay",
                "expect_bundle": "--expect-bundle",
                "expect_artifact": "--expect-artifact",
                "json_output": "--json",
            },
        )
        return _run_worker_once()
    if args.replay is not None:
        _require_nonempty_option(parser, args.replay, "--replay")
        _require_nonempty_option(
            parser,
            args.expect_bundle,
            "--expect-bundle",
        )
        _require_nonempty_option(
            parser,
            args.expect_artifact,
            "--expect-artifact",
        )
        _reject_present_options(
            args,
            parser,
            "--replay",
            {
                **EXECUTION_OPTION_FLAGS,
                "script": "script",
                "expression": "--eval",
                "jsonl": "--jsonl",
                "doctor": "--doctor",
                "doctor_mode": "--doctor-mode",
                "expect_inventory": "--expect-inventory",
                "export_replay": "--export-replay",
            },
        )
        return _run_replay(
            args.replay,
            json_output=args.json_output,
            expected_bundle=args.expect_bundle,
            expected_artifact=args.expect_artifact,
        )
    if args.doctor is not None:
        _require_nonempty_option(
            parser,
            args.expect_inventory,
            "--expect-inventory",
        )
        _require_nonempty_option(
            parser,
            args.export_replay,
            "--export-replay",
        )
        _reject_present_options(
            args,
            parser,
            "--doctor",
            {
                **EXECUTION_OPTION_FLAGS,
                "script": "script",
                "expression": "--eval",
                "jsonl": "--jsonl",
                "replay": "--replay",
                "expect_bundle": "--expect-bundle",
                "expect_artifact": "--expect-artifact",
            },
        )
        v3_profiles = {"inventory@1", "replay@2", "effects@2", "release@2"}
        if args.doctor in v3_profiles and args.doctor_mode is None:
            parser.error("doctor v3 profiles require --doctor-mode")
        if args.doctor not in v3_profiles and (
            args.doctor_mode is not None or args.expect_inventory is not None
        ):
            parser.error(
                "--doctor-mode/--expect-inventory require a doctor v3 profile"
            )
        return _run_doctor(
            profile=args.doctor,
            json_output=args.json_output,
            export_path=args.export_replay,
            mode=args.doctor_mode,
            expected_inventory=args.expect_inventory,
        )
    if args.export_replay is not None:
        parser.error("--export-replay requires --doctor")
    if args.expect_bundle is not None or args.expect_artifact is not None:
        parser.error("--expect-bundle/--expect-artifact require --replay")
    if args.doctor_mode is not None or args.expect_inventory is not None:
        parser.error("--doctor-mode/--expect-inventory require --doctor")
    if args.json_output:
        parser.error("--json requires --doctor or --replay")
    if args.jsonl:
        _reject_present_options(
            args,
            parser,
            "--jsonl",
            {
                **EXECUTION_OPTION_FLAGS,
                "script": "script",
                "expression": "--eval",
                "doctor": "--doctor",
                "doctor_mode": "--doctor-mode",
                "expect_inventory": "--expect-inventory",
                "export_replay": "--export-replay",
                "replay": "--replay",
                "expect_bundle": "--expect-bundle",
                "expect_artifact": "--expect-artifact",
                "json_output": "--json",
            },
        )
        return _run_jsonl_supervisor()
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
        parser.error(
            "--unlimited cannot be combined with: "
            + ", ".join(sorted(explicit_limits))
        )
    if args.state_dir is not None:
        set_state_dir(args.state_dir)

    try:
        limits = (
            ExecutionLimits.unlimited()
            if args.unlimited
            else ExecutionLimits(
                **{
                    name: (
                        getattr(args, name)
                        if getattr(args, name) is not None
                        else default
                    )
                    for name, default in EXECUTION_LIMIT_DEFAULTS.items()
                }
            )
        )
        env = make_global_env(
            trusted=args.trusted,
            load_stdlib=not args.no_stdlib,
            limits=limits,
        )

        if args.expression is not None:
            _print_result(
                run_string(
                    args.expression,
                    env,
                    source_name="<command-line>",
                ),
                env.writer,
            )
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
                    raw = (
                        stream.read()
                        if limit is None
                        else stream.read(limit + 1)
                    )
                    source = raw.decode("utf-8")
            except UnicodeError as exc:
                raise LispSyntaxError(
                    f"stdin is not valid text: {exc}",
                    source_name="<stdin>",
                )
            if limit is not None and len(raw) > limit:
                raise ExecutionLimitExceeded(
                    "source_bytes",
                    limit,
                    len(raw),
                )
            if source.strip():
                _print_result(
                    run_string(source, env, source_name="<stdin>"),
                    env.writer,
                )
            return 0

        repl(env)
        return 0
    except LispError as exc:
        _render_cli_error(args, exc)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
