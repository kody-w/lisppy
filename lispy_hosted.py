"""Optional hosting, worker isolation, diagnostics, and compatibility bindings for LisPy."""

from __future__ import annotations

import base64
import csv
import hashlib
import importlib.metadata
import json
import math
import os
import re
import signal
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

import lisp as _language
from lisp import (
    BoundedOutput,
    CapabilityDenied,
    DISTRIBUTION_NAME,
    EXECUTION_OPTION_FLAGS,
    ExecutionLimitExceeded,
    ExecutionLimits,
    InvalidDataError,
    LANGUAGE_PROFILE,
    LispError,
    LispSyntaxError,
    NIL,
    Pair,
    REPO_ROOT,
    STDLIB_PATH,
    Symbol,
    VERSION,
    WORKER_JSON_MAX_DEPTH,
    WORKER_JSON_MAX_NODES,
    _denied,
    _require_nonempty_option,
    _resource_bytes,
    _resource_text,
    _strict_json_loads,
    _validate_value_graph,
    json_to_lisp,
    make_global_env,
    py_to_lisp,
    run_string,
)


class HostedValidationError(LispError):
    category = "validation"

    def __init__(self, violations):
        self.violations = list(violations)
        super().__init__("; ".join(str(item) for item in self.violations))


def _state_path(filename: str, state_dir: str | Path | None = None) -> Path:
    """Resolve a state path without allowing escapes from STATE_DIR."""
    root = Path(state_dir if state_dir is not None else _language.STATE_DIR).resolve()
    path = (root / filename).resolve()
    try:
        path.relative_to(root)
    except ValueError:
        raise CapabilityDenied(f"state path escapes configured root: {filename}")
    return path


def rb_state(filename: str, *, state_dir=None) -> Any:
    """Read a state JSON file and return as Lisp value."""
    path = _state_path(filename, state_dir)
    try:
        with open(path, "rb") as file:
            raw = file.read(_language.MAX_STATE_BYTES + 1)
        if len(raw) > _language.MAX_STATE_BYTES:
            raise InvalidDataError(f"state file exceeds {_language.MAX_STATE_BYTES} bytes: {path}")
        data = _strict_json_loads(raw.decode("utf-8"))
        return json_to_lisp(data)
    except FileNotFoundError:
        raise LispError(f"state file not found: {path}")
    except (UnicodeDecodeError, ValueError, json.JSONDecodeError) as e:
        raise InvalidDataError(f"invalid JSON in {path}: {e}")


def rb_agent(agent_id: str, *, state_dir=None) -> Any:
    """Get an agent profile by ID."""
    agents = rb_state("agents.json", state_dir=state_dir)
    if not isinstance(agents, dict) or not isinstance(agents.get("agents"), dict):
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
        or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", agent_id) is None
    ):
        raise InvalidDataError("agent id is not canonical")
    path = _state_path(f"memory/{agent_id}.md", state_dir)
    try:
        with open(path, "rb") as file:
            content = file.read(_language.MAX_STATE_BYTES + 1)
    except FileNotFoundError:
        return f"(no soul file for {agent_id})"
    if len(content) > _language.MAX_STATE_BYTES:
        raise ExecutionLimitExceeded("state_bytes", _language.MAX_STATE_BYTES, len(content))
    try:
        return content.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise InvalidDataError(f"invalid UTF-8 soul file: {exc}")


def rb_channels(*, state_dir=None) -> Any:
    """Get all channels as a list of dicts."""
    data = rb_state("channels.json", state_dir=state_dir)
    if not isinstance(data, dict) or not isinstance(data.get("channels"), dict):
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
    if not isinstance(data, dict) or not isinstance(data.get("trending"), list):
        raise LispError("trending.json must contain a 'trending' list")
    return json_to_lisp(data["trending"])


def _required_text(value, field):
    if isinstance(value, Symbol) or not isinstance(value, str) or not value.strip():
        raise LispError(f"{field} must be a non-empty string")
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
        "payload": {"discussion_number": discussion_number, "body": _required_text(body, "body")},
    }


def rb_react(node_id: str, reaction: str) -> dict:
    """Create a typed dry-run reaction intent."""
    allowed = {"thumbs_up", "thumbs_down", "laugh", "hooray", "confused", "heart", "rocket", "eyes"}
    reaction = _required_text(reaction, "reaction").lower()
    if reaction not in allowed:
        raise LispError(f"unsupported reaction: {reaction}")
    return {
        "type": "rappterbook.reaction.add",
        "payload": {"node_id": _required_text(node_id, "node_id"), "reaction": reaction},
    }


def effect_idempotency_key(contract_id, intent_scope, sequence):
    material = json.dumps(
        [contract_id, intent_scope, sequence], ensure_ascii=False, separators=(",", ":")
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
                [sys.executable, "-c", code], capture_output=True, text=True, timeout=10
            )
        else:
            result = subprocess.run(
                ["bash", str(script), code], capture_output=True, text=True, timeout=10
            )
    except subprocess.TimeoutExpired:
        raise LispError("trusted Python execution timed out")
    except OSError as exc:
        raise LispError(f"trusted Python execution failed: {exc}")

    if result.returncode != 0:
        message = result.stderr.strip() or "no error output"
        raise LispError(f"trusted Python exited {result.returncode}: {message}")
    return result.stdout


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


def _copy_host_value(value: Any, path: str = "$", seen: set[int] | None = None) -> Any:
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
                _copy_host_value(item, f"{path}[{index}]", seen) for index, item in enumerate(value)
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


def _receipt_value(value: Any, path: str = "$", *, _validated=False) -> Any:
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
            items.append(_receipt_value(current.car, f"{path}[{len(items)}]", _validated=True))
            current = current.cdr
        if current is not NIL:
            raise TypeError(f"{path}: improper lists are not JSON receipt values")
        return items
    if isinstance(value, list):
        return [
            _receipt_value(item, f"{path}[{index}]", _validated=True)
            for index, item in enumerate(value)
        ]
    if isinstance(value, dict):
        converted = {}
        for key, item in value.items():
            if not isinstance(key, str) or isinstance(key, Symbol):
                raise TypeError(f"{path}: object keys must be strings")
            converted[key] = _receipt_value(item, f"{path}.{key}", _validated=True)
        return converted
    raise TypeError(f"{path}: unsupported LisPy value {type(value).__name__}")


def _hosted_error_receipt(base, output, phase, error, *, context=None):
    details = {
        "phase": phase,
        "type": type(error).__name__,
        "category": getattr(error, "category", "host"),
        "message": str(error),
    }
    if isinstance(error, ExecutionLimitExceeded):
        details.update({"resource": error.resource, "limit": error.limit, "used": error.used})
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
            {"steps": context.steps, "peak_call_depth": context.peak_call_depth}
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
        "runtime": {"version": VERSION, "profile": LANGUAGE_PROFILE},
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
        if isinstance(max_effects, bool) or not isinstance(max_effects, int) or max_effects < 1:
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
            raise ValueError("input/output bindings overlap: " + ", ".join(sorted(overlap)))
    except (TypeError, ValueError) as exc:
        return _hosted_error_receipt(base, output, "input", exc)

    try:
        env = make_global_env(trusted=False, limits=effective_limits, output=output, profile="core")
    except LispError as exc:
        return _hosted_error_receipt(base, output, "prepare", exc)

    hosted_capabilities = ["lispy-core@1", "console.capture", "hosted-governor@2"]
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
        "profiles": [LANGUAGE_PROFILE, "hosted-governor@2"],
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
                raise ExecutionLimitExceeded("effects", max_effects, sequence + 1)
            effect = {
                "sequence": sequence,
                "type": intent["type"],
                "payload": _copy_host_value(intent["payload"]),
                "mode": "dry_run",
                "applied": False,
                "idempotency_key": effect_idempotency_key(contract_id, intent_scope, sequence),
                "effect_sha256": effect_digest(source_hash, intent["type"], intent["payload"]),
            }
            effects.append(effect)
            return effect

        env["rb-post"] = lambda channel, title, body: collect(rb_post(channel, title, body))
        env["rb-comment"] = lambda number, body: collect(rb_comment(number, body))
        env["rb-react"] = lambda node_id, reaction: collect(rb_react(node_id, reaction))

    existing = set(env)
    collisions = existing & (set(copied_inputs) | set(copied_outputs))
    if collisions:
        error = ValueError(
            "host bindings collide with runtime names: " + ", ".join(sorted(collisions))
        )
        return _hosted_error_receipt(base, output, "input", error, context=env.context)

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
        result = run_string(source, env, source_name=f"<hosted:{contract_id}>")
        outputs = {name: _receipt_value(env[name], f"$.outputs.{name}") for name in copied_outputs}
        converted_result = _receipt_value(result, "$.result")
    except LispSyntaxError as exc:
        return _hosted_error_receipt(base, output, "parse", exc, context=env.context)
    except LispError as exc:
        return _hosted_error_receipt(base, output, "evaluate", exc, context=env.context)
    except (TypeError, ValueError) as exc:
        return _hosted_error_receipt(base, output, "serialize", exc, context=env.context)

    if validate is not None:
        try:
            violations = validate(_copy_host_value(copied_inputs), _copy_host_value(outputs))
            if violations:
                if isinstance(violations, str):
                    violations = [violations]
                raise HostedValidationError(violations)
        except HostedValidationError as exc:
            return _hosted_error_receipt(base, output, "validate", exc, context=env.context)
        except LispError as exc:
            return _hosted_error_receipt(base, output, "validate", exc, context=env.context)
        except (AttributeError, KeyError, RuntimeError, TypeError, ValueError) as exc:
            return _hosted_error_receipt(base, output, "validate", exc, context=env.context)

    if validate_effects is not None:
        try:
            violations = validate_effects(_copy_host_value(effects))
            if violations:
                if isinstance(violations, str):
                    violations = [violations]
                raise HostedValidationError(violations)
        except HostedValidationError as exc:
            return _hosted_error_receipt(base, output, "validate", exc, context=env.context)
        except LispError as exc:
            return _hosted_error_receipt(base, output, "validate", exc, context=env.context)
        except (AttributeError, KeyError, RuntimeError, TypeError, ValueError) as exc:
            return _hosted_error_receipt(base, output, "validate", exc, context=env.context)

    receipt = {
        **base,
        "status": "accepted",
        "outputs": outputs,
        "writes": sorted(env.tracked_writes),
        "result": converted_result,
        "logs": output.getvalue(),
        "usage": {"steps": env.context.steps, "peak_call_depth": env.context.peak_call_depth},
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




DOCTOR_V3_MAX_REPORT_BYTES = 65_536


def _protocol_error(request_id, code, message):
    return {
        "api": WORKER_API,
        "id": request_id,
        "ok": False,
        "error": {"code": code, "message": str(message)},
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
        raise ValueError("unknown limit fields: " + ", ".join(sorted(unknown)))
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
    required = {"heating_alloc", "isru_alloc", "greenhouse_alloc", "food_ration"}
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
        not 0 <= outputs[name] <= 1 for name in ("heating_alloc", "isru_alloc", "greenhouse_alloc")
    ):
        return ["allocation values must be between 0 and 1"]
    if not 0.08 <= outputs["food_ration"] <= 1.5:
        return ["food_ration must be between 0.08 and 1.5"]
    total = sum(outputs[name] for name in ("heating_alloc", "isru_alloc", "greenhouse_alloc"))
    if abs(total - 1.0) > 1e-9:
        return ["heating/isru/greenhouse allocations must sum to 1"]
    return []


def _registered_sources():
    definitions = {
        "mars-barn/governor-example": {
            "path": REPO_ROOT / "examples" / "mars-barn" / "mars-colony-governor.lispy",
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
            "path": REPO_ROOT / "examples" / "hosted-governor.lispy",
            "resource": "registered/hosted-governor.lisp",
            "profile": "hosted-governor@2",
            "contract_id": "mars-barn/governor-controls@1",
            "required_inputs": ["sol"],
            "effect_types": ["rappterbook.post.create"],
        },
    }
    registered = {}
    for source_id, definition in definitions.items():
        source = _resource_text(definition["resource"], definition["path"])
        registered[source_id] = {
            **{key: value for key, value in definition.items() if key not in ("path", "resource")},
            "source": source,
            "source_sha256": hashlib.sha256(source.encode("utf-8")).hexdigest(),
        }
    return registered


def registered_source(source_id):
    registered = _registered_sources()
    if source_id not in registered:
        raise InvalidDataError(f"unknown registered source: {source_id}")
    return _copy_host_value(registered[source_id])


def run_registered_governor(
    source_id, *, expected_source_sha256, inputs, mutable_outputs, intent_scope=None, limits=None
):
    source = registered_source(source_id)
    if (
        not isinstance(expected_source_sha256, str)
        or re.fullmatch(r"[0-9a-f]{64}", expected_source_sha256) is None
    ):
        raise InvalidDataError("expected_source_sha256 must be lowercase SHA-256")
    if source["source_sha256"] != expected_source_sha256:
        raise InvalidDataError("registered source SHA-256 mismatch")
    if not isinstance(inputs, dict):
        raise InvalidDataError("registered governor inputs must be an object")
    missing = set(source["required_inputs"]) - set(inputs)
    if missing:
        raise InvalidDataError("registered governor missing inputs: " + ", ".join(sorted(missing)))
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
        "runtime": Path(_language.__file__).read_bytes(),
        "hosting": Path(__file__).read_bytes(),
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
            "profiles": [LANGUAGE_PROFILE, "hosted-governor@2"],
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
    if not isinstance(request_id, str) or not request_id or len(request_id.encode("utf-8")) > 256:
        return _protocol_error(None, "invalid_request", "id must be 1-256 UTF-8 bytes")
    if request.get("api") != WORKER_API:
        return _protocol_error(request_id, "invalid_api", "unsupported worker API")

    operation = request.get("op")
    try:
        if operation == "manifest":
            _validate_request_fields(
                request, allowed={"api", "id", "op"}, required={"api", "id", "op"}
            )
            return {"api": WORKER_API, "id": request_id, "ok": True, "manifest": _worker_manifest()}

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
            raise ValueError("registered source_id is required for this contract")
        if "source" in request:
            raise ValueError("inline source is not allowed with registered source_id")
        registered = _registered_sources()
        if source_id not in registered:
            raise ValueError(f"unknown source_id: {source_id}")
        source_entry = registered[source_id]
        if source_entry["contract_id"] != contract_id:
            raise ValueError("source contract mismatch")
        expected_hash = request.get("expected_source_sha256")
        if not isinstance(expected_hash, str) or not re.fullmatch(r"[0-9a-f]{64}", expected_hash):
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
            {key: value for key, value in request.items() if key != "id"}
        )
        receipt["outcome_sha256"] = "sha256:" + _canonical_sha256(receipt)
        return {"api": WORKER_API, "id": request_id, "ok": True, "receipt": receipt}
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
    return json.dumps(value, allow_nan=False, ensure_ascii=False, separators=(",", ":")) + "\n"


def _worker_environment(temp_root=None):
    environment = {"PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"}
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
            args=(process.stdout, WORKER_MAX_RESPONSE_BYTES, stdout_state, event),
            daemon=True,
        ),
        threading.Thread(
            target=_pump_worker_pipe,
            args=(process.stderr, WORKER_MAX_STDERR_BYTES, stderr_state, event),
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
                if (
                    isinstance(preview, dict)
                    and isinstance(preview.get("id"), str)
                    and len(preview["id"].encode("utf-8")) <= 256
                ):
                    request_id = preview["id"]
                    request_operation = preview.get("op")
            except (UnicodeDecodeError, ValueError, json.JSONDecodeError):
                pass

        if oversized:
            response = _protocol_error(request_id, "line_too_large", "request line too large")
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
                            str(Path(_language.__file__).resolve()),
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
                        request_id, "worker_spawn_failed", "worker process could not start"
                    )
                else:
                    supervised = _supervise_worker(process, line)
                    cause = supervised["cause"]
                    stdout = supervised["stdout"]
                    if cause == "timeout":
                        response = _protocol_error(
                            request_id, "timeout", "worker exceeded wall deadline"
                        )
                    elif cause == "stdout_overflow":
                        response = _protocol_error(
                            request_id, "response_too_large", "worker response too large"
                        )
                    elif cause is not None:
                        response = _protocol_error(
                            request_id, "worker_crash", "worker process failed"
                        )
                    elif supervised["returncode"] != 0:
                        response = _protocol_error(
                            request_id, "worker_crash", "worker process failed"
                        )
                    else:
                        try:
                            response = _strict_json_loads(stdout.decode("utf-8"))
                        except (UnicodeDecodeError, ValueError, json.JSONDecodeError) as exc:
                            response = _protocol_error(request_id, "invalid_worker_response", exc)
                        if not _validate_worker_response_shape(
                            response, request_id, request_operation
                        ):
                            response = _protocol_error(
                                request_id,
                                "invalid_worker_response",
                                "worker response correlation failed",
                            )
        response_line = _json_line(response)
        if len(response_line.encode("utf-8")) > WORKER_MAX_RESPONSE_BYTES:
            response_line = _json_line(
                _protocol_error(None, "response_too_large", "supervisor response too large")
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
        content = _resource_bytes(f"sample-state/{name}", root / name)
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
                str(Path(_language.__file__).resolve()),
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
        raise LispError(f"doctor worker exited {process.returncode}: {process.stderr.strip()}")
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
        value, allow_nan=False, ensure_ascii=False, sort_keys=True, separators=(",", ":")
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
        raise InvalidDataError(f"{name} must be lowercase SHA-256")
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
        {key: value for key, value in request.items() if key != "id"}
    )
    if receipt.get("request_sha256") != expected_request:
        raise InvalidDataError("recorded request digest mismatch")
    outcome_payload = {key: value for key, value in receipt.items() if key != "outcome_sha256"}
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
        (item for item in manifest.get("sources", []) if item.get("id") == request["source_id"]),
        None,
    )
    checks = {
        "source_manifest_match": bool(
            source
            and receipt
            and receipt.get("source_id") == source["id"]
            and receipt.get("source_sha256") == source["source_sha256"]
            and request["expected_source_sha256"] == source["source_sha256"]
            and receipt.get("artifact_sha256") == manifest.get("artifact_sha256")
        ),
        "receipt_accepted": bool(receipt and receipt.get("status") == "accepted"),
        "effects_dry_run": bool(
            len(effects) == 1
            and effects[0].get("mode") == "dry_run"
            and effects[0].get("applied") is False
            and effects[0].get("type") == "rappterbook.post.create"
            and effects[0].get("idempotency_key")
            == effect_idempotency_key(receipt["contract_id"], receipt["intent_scope"], 0)
            and effects[0].get("effect_sha256")
            == effect_digest(receipt["source_sha256"], effects[0]["type"], effects[0]["payload"])
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
                report, allow_nan=False, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            )
            if len((encoded + "\n").encode("utf-8")) > DOCTOR_V3_MAX_REPORT_BYTES:
                raise InvalidDataError("doctor v3 report exceeds byte limit")
            sys.stdout.write(encoded + "\n")
            return
        print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    else:
        print(f"LisPy doctor: {'PASS' if report['ok'] else 'FAIL'}")
        if "checks" not in report:
            for name, component in report["components"].items():
                if component is not None:
                    print(f"  {'ok' if component['ok'] else 'FAIL'}  {name}")
            return
        checks = report["checks"]
        if isinstance(checks, dict):
            for name, passed in checks.items():
                print(f"  {'ok' if passed else 'FAIL'}  {name}")
        else:
            for check in checks:
                print(f"  {'ok' if check['ok'] else 'FAIL'}  {check['id']}")


def _collect_replay_evidence():
    before = _sample_state_sha256()
    manifest_response = _doctor_worker_request(
        {"api": WORKER_API, "id": "doctor-manifest", "op": "manifest"}
    )
    if not manifest_response.get("ok"):
        raise LispError("doctor could not read worker manifest")
    manifest = manifest_response["manifest"]
    source = next(item for item in manifest["sources"] if item["id"] == "lispy/hosted-doctor@1")
    request = _doctor_request(source["source_sha256"])
    first = _doctor_worker_request(request)
    second = _doctor_worker_request(request)
    after = _sample_state_sha256()
    return manifest, request, first, second, before, after


def _doctor_check(check_id, ok, *, details=None, error=None):
    return {"id": check_id, "ok": bool(ok), "details": details or {}, "error": error}


INSTALLED_REQUIRED_PATHS = (
    "lisp.py",
    "lispy_hosted.py",
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
    "lisppy/data/llms.txt",
)


BUILD_INPUT_PATHS = ("MANIFEST.in", "README.md", "pyproject.toml", "setup.cfg", "setup.py")


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
        if not size_field.isdigit() or str(int(size_field)) != size_field:
            raise InvalidDataError(f"RECORD size is invalid: {relative}")
        if not hash_field.startswith("sha256="):
            raise InvalidDataError(f"RECORD missing SHA-256: {relative}")
        path = Path(distribution.locate_file(relative)).resolve()
        content = path.read_bytes()
        expected_hash = hash_field.split("=", 1)[1]
        actual_hash = (
            base64.urlsafe_b64encode(hashlib.sha256(content).digest()).decode("ascii").rstrip("=")
        )
        if actual_hash != expected_hash or int(size_field) != len(content):
            raise InvalidDataError(f"RECORD hash mismatch: {relative}")
        artifacts.append(
            {"path": relative, "size": len(content), "sha256": hashlib.sha256(content).hexdigest()}
        )
    for relative, (hash_field, size_field) in rows.items():
        if relative in INSTALLED_REQUIRED_PATHS:
            continue
        if (
            not hash_field
            and not size_field
            and (relative.endswith(".pyc") or relative.endswith(".dist-info/RECORD"))
        ):
            continue
        _verify_record_content(distribution, relative, hash_field, size_field)
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
    actual_hash = (
        base64.urlsafe_b64encode(hashlib.sha256(content).digest()).decode("ascii").rstrip("=")
    )
    if actual_hash != hash_field.split("=", 1)[1] or int(size_field) != len(content):
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
        canonical = f"{directory}/__pycache__/{name}" if directory else f"__pycache__/{name}"
        cached = module
        pyc_patterns.extend(
            [
                re.compile(r"^" + re.escape(canonical) + r"\.cpython-[0-9]+\.pyc$"),
                re.compile(
                    r"^(?:\.\./)+.+/site-packages/" + re.escape(cached) + r"\.cpython-[0-9]+\.pyc$"
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
        raise InvalidDataError(f"RECORD contains unexpected installed path: {path}")


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
                    error={"category": "package", "code": "distribution_not_installed"},
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
                details={"expected": VERSION, "actual": distribution.version},
            )
        )
        try:
            import effect_executor as executor_module
            import lisppy as package_module

            origins = {
                "lisp.py": Path(_language.__file__).resolve(),
                "lispy_hosted.py": Path(__file__).resolve(),
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
                len(console_entries) == 1 and console_entries[0].value == "lisppy.cli:main",
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
            checks.append(_doctor_check("distribution.resources", False))
            checks.append(_doctor_check("distribution.inventory", False))
        else:
            checks.append(_doctor_check("distribution.record", True))
            checks.append(
                _doctor_check(
                    "distribution.resources", len(artifacts) == len(INSTALLED_REQUIRED_PATHS)
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

    manifest, request, first, second, before, after = evidence or _collect_replay_evidence()
    replay_report = _doctor_report(manifest, request, first, second, before, after)
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
        "adapter_ids": {"rappterbook.post.create": "doctor-recording@1"},
    }
    store = InMemoryIdempotencyStore()
    options = {"expected": pins, "registry": registry, "store": store, "namespace": "doctor"}
    first_execution = execute_effects_batch(receipt, execution_id="doctor-effects-first", **options)
    second_execution = execute_effects_batch(
        receipt, execution_id="doctor-effects-second", **options
    )
    checks = [
        _doctor_check("effects.replay-prerequisite", replay_report["ok"]),
        _doctor_check(
            "effects.first-application",
            [item["status"] for item in first_execution["effects"]] == ["applied"],
        ),
        _doctor_check(
            "effects.idempotent-replay",
            [item["status"] for item in second_execution["effects"]] == ["duplicate_applied"]
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
            content = _resource_bytes(relative[len("lisppy/data/") :], path)
        else:
            content = path.read_bytes()
        files.append(
            {"path": relative, "size": len(content), "sha256": hashlib.sha256(content).hexdigest()}
        )
    return sorted(files, key=lambda item: item["path"])


def build_source_sha256():
    files = []
    for relative in BUILD_INPUT_PATHS:
        content = (REPO_ROOT / relative).read_bytes()
        files.append(
            {"path": relative, "size": len(content), "sha256": hashlib.sha256(content).hexdigest()}
        )
    return _canonical_sha256({"api": "lispy.build-source/v1", "files": files})


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
            error = {"category": "doctor", "code": "check_failed"}
        normalized.append({"id": check["id"], "ok": ok, "error": error})
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
                "error": {"category": "doctor", "code": code},
            }
        ],
        "evidence": {},
    }


DOCTOR_V3_CHECK_CATALOGS = {
    "inventory": {
        "source": ("inventory.source-files", "inventory.external-pin"),
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
    if not {"category", "code"} <= set(error) <= {"category", "code", "message"}:
        raise InvalidDataError("invalid doctor v3 error fields")
    if (
        not isinstance(error["category"], str)
        or not error["category"]
        or not isinstance(error["code"], str)
        or not error["code"]
        or ("message" in error and (not isinstance(error["message"], str) or not error["message"]))
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
        or set(runtime) != {"distribution", "version", "language_profile"}
        or runtime["distribution"] != DISTRIBUTION_NAME
        or runtime["version"] != VERSION
        or runtime["language_profile"] != LANGUAGE_PROFILE
    ):
        raise InvalidDataError("invalid doctor v3 runtime")
    pins = report["pins"]
    if not isinstance(pins, dict) or set(pins) != {"inventory_sha256", "actual_inventory_sha256"}:
        raise InvalidDataError("invalid doctor v3 pins")
    for value in pins.values():
        if value is not None:
            _validate_doctor_v3_sha256(value, "pin")
    components = report["components"]
    if not isinstance(components, dict) or set(components) != {"inventory", "replay", "effects"}:
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
            _validate_doctor_v3_error(check["error"], required=not check["ok"])
            if check["ok"] and check["error"] is not None:
                raise InvalidDataError("successful doctor v3 check has an error")
        execution_catalog = [f"{name}.execution"]
        expected_catalog = (
            list(DOCTOR_V3_CHECK_CATALOGS["inventory"][report["mode"]])
            if name == "inventory"
            else list(DOCTOR_V3_CHECK_CATALOGS[name])
        )
        if check_ids not in (execution_catalog, expected_catalog):
            raise InvalidDataError("invalid doctor v3 check catalog")
        if check_ids == execution_catalog and (
            component["ok"] or component["checks"][0]["ok"] or component["evidence"] != {}
        ):
            raise InvalidDataError("invalid doctor v3 component execution failure")
        if component["ok"] != all(check["ok"] for check in component["checks"]):
            raise InvalidDataError("doctor v3 component status mismatch")
    aggregate = all(component is None or component["ok"] for component in components.values())
    if report["ok"] != aggregate:
        raise InvalidDataError("doctor v3 aggregate status mismatch")
    if (report["error"] is None) != report["ok"]:
        raise InvalidDataError("doctor v3 error status mismatch")
    if report["ok"]:
        _validate_doctor_v3_error(report["error"], required=False)
    elif report["error"] != {"category": "doctor", "code": "component_failure"}:
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
        _validate_doctor_v3_sha256(artifact["sha256"], "artifact SHA-256")
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
            != {"api", "distribution", "version", "language_profile", "files", "sha256"}
            or inventory["api"] != "lispy.inventory/v1"
            or inventory["distribution"] != DISTRIBUTION_NAME
            or inventory["version"] != VERSION
            or inventory["language_profile"] != LANGUAGE_PROFILE
            or inventory["files"] != artifacts
        ):
            raise InvalidDataError("doctor v3 inventory/artifact mismatch")
        payload = {key: value for key, value in inventory.items() if key != "sha256"}
        if inventory.get("sha256") != _canonical_sha256(payload):
            raise InvalidDataError("doctor v3 inventory digest mismatch")
        actual = report["pins"]["actual_inventory_sha256"]
        if actual != inventory["sha256"]:
            raise InvalidDataError("doctor v3 pin/inventory mismatch")
        external_pin_check = inventory_component["checks"][-1]
        expected_pin = report["pins"]["inventory_sha256"]
        pin_matches = expected_pin is None or expected_pin == inventory["sha256"]
        if external_pin_check["id"] != "inventory.external-pin":
            raise InvalidDataError("missing doctor v3 inventory pin check")
        if external_pin_check["ok"] != pin_matches:
            raise InvalidDataError("doctor v3 inventory pin status mismatch")
        if not pin_matches and (
            external_pin_check["error"] is None
            or external_pin_check["error"].get("code") != "inventory_pin_mismatch"
        ):
            raise InvalidDataError("doctor v3 inventory pin error mismatch")
    elif (
        artifacts
        or report["pins"]["actual_inventory_sha256"] is not None
        or [check["id"] for check in inventory_component["checks"]] != ["inventory.execution"]
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
                _validate_doctor_v3_sha256(value, "replay evidence SHA-256")
            replay_checks = {check["id"]: check for check in replay_component["checks"]}
            evidence = replay_component["evidence"]
            if replay_checks["state_unchanged"]["ok"] != (
                evidence["state_before_sha256"] == evidence["state_after_sha256"]
            ):
                raise InvalidDataError("doctor v3 state evidence mismatch")
            if replay_checks["replay_match"]["ok"] != (
                evidence["first_response_sha256"] == evidence["second_response_sha256"]
            ):
                raise InvalidDataError("doctor v3 replay evidence mismatch")

    effects_component = components["effects"]
    if effects_component is not None:
        effects_ids = [check["id"] for check in effects_component["checks"]]
        if effects_ids == list(DOCTOR_V3_CHECK_CATALOGS["effects"]):
            if set(effects_component["evidence"]) != {"checks_sha256"}:
                raise InvalidDataError("invalid doctor v3 effects evidence")
            _validate_doctor_v3_sha256(
                effects_component["evidence"]["checks_sha256"], "effects evidence SHA-256"
            )
            if effects_component["evidence"]["checks_sha256"] != _canonical_sha256(
                effects_component["checks"]
            ):
                raise InvalidDataError("doctor v3 effects evidence digest mismatch")
    encoded = json.dumps(
        report, allow_nan=False, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    if len(encoded) + 1 > DOCTOR_V3_MAX_REPORT_BYTES:
        raise InvalidDataError("doctor v3 report exceeds byte limit")
    return report


def _doctor_v3(profile, mode, expected_inventory):
    if mode not in ("source", "installed"):
        raise LispError("doctor v3 requires --doctor-mode source or installed")
    expected_pin = _normalize_sha256_pin(expected_inventory, "expected inventory pin")
    components = {"inventory": None, "replay": None, "effects": None}
    files = []
    replay_evidence = None
    try:
        if mode == "source":
            files = _source_inventory()
            inventory_checks = [_doctor_check("inventory.source-files", True)]
        else:
            inventory_checks, files = _installed_doctor_profile()
        inventory = _inventory_payload(files)
        pin_ok = expected_pin is None or expected_pin == inventory["sha256"]
        inventory_checks.append(
            _doctor_check(
                "inventory.external-pin",
                pin_ok,
                error=(
                    None if pin_ok else {"category": "package", "code": "inventory_pin_mismatch"}
                ),
            )
        )
        components["inventory"] = _v3_component(
            "inventory", inventory_checks, {"inventory": inventory}
        )
    except Exception:
        inventory = None
        files = []
        components["inventory"] = _v3_failed_component("inventory", "inventory_component_failed")
    if profile in ("replay@2", "release@2"):
        try:
            replay_evidence = _collect_replay_evidence()
            manifest, request, first, second, before, after = replay_evidence
            replay = _doctor_report(manifest, request, first, second, before, after)
            components["replay"] = _v3_component(
                "replay",
                [_doctor_check(name, passed) for name, passed in replay["checks"].items()],
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
            components["replay"] = _v3_failed_component("replay", "replay_component_failed")
    if profile in ("effects@2", "release@2"):
        try:
            effect_checks = _effects_doctor_profile(replay_evidence)
            effect_component = _v3_component("effects", effect_checks, {})
            effect_component["evidence"] = {
                "checks_sha256": _canonical_sha256(effect_component["checks"])
            }
            components["effects"] = effect_component
        except Exception:
            components["effects"] = _v3_failed_component("effects", "effects_component_failed")
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
            "actual_inventory_sha256": (inventory["sha256"] if inventory is not None else None),
        },
        "ok": all(component is None or component["ok"] for component in components.values()),
        "components": components,
        "artifacts": files,
        "error": (
            None
            if all(component is None or component["ok"] for component in components.values())
            else {"category": "doctor", "code": "component_failure"}
        ),
    }
    return _validate_doctor_v3_report(report)


def _run_doctor(
    *, profile="replay@1", json_output=False, export_path=None, mode=None, expected_inventory=None
):
    if profile in ("inventory@1", "replay@2", "effects@2", "release@2"):
        if export_path is not None:
            raise LispError("--export-replay is supported only by replay@1")
        report = _doctor_v3(profile, mode, expected_inventory)
        _print_doctor_report(report, json_output)
        return 0 if report["ok"] else 1
    if profile != "replay@1":
        if export_path is not None:
            raise LispError("--export-replay is supported only by replay@1")
        report = _doctor_v2(profile)
        _print_doctor_report(report, json_output)
        return 0 if report["ok"] else 1

    manifest, request, first, second, before, after = _collect_replay_evidence()
    report = _doctor_report(manifest, request, first, second, before, after)
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
        bundle = {**payload, "bundle_sha256": _canonical_sha256(payload)}
        destination = Path(export_path)
        temporary = destination.with_name(destination.name + ".tmp")
        temporary.write_text(
            json.dumps(bundle, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        temporary.replace(destination)
        report["replay_bundle"] = str(destination)
        report["bundle_sha256"] = bundle["bundle_sha256"]
    _print_doctor_report(report, json_output)
    return 0 if report["ok"] else 1


def _run_replay(path, *, json_output=False, expected_bundle=None, expected_artifact=None):
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
    payload = {key: value for key, value in bundle.items() if key != "bundle_sha256"}
    if digest != _canonical_sha256(payload):
        raise InvalidDataError("replay bundle digest mismatch")
    expected_bundle = _normalize_sha256_pin(expected_bundle, "expected bundle pin")
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
    expected_artifact = _normalize_sha256_pin(expected_artifact, "expected artifact pin")
    if expected_artifact is not None and bundled_artifact != expected_artifact:
        raise InvalidDataError("external artifact pin mismatch")
    _verify_worker_response_digests(bundle["first_response"], request, bundled_artifact)
    _verify_worker_response_digests(bundle["second_response"], request, bundled_artifact)

    current_manifest_response = _doctor_worker_request(
        {"api": WORKER_API, "id": "doctor-manifest", "op": "manifest"}
    )
    current_manifest = current_manifest_response.get("manifest")
    before = _sample_state_sha256()
    first = _doctor_worker_request(request)
    second = _doctor_worker_request(request)
    after = _sample_state_sha256()
    semantic_report = _doctor_report(current_manifest, request, first, second, before, after)
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


def _reject_present_options(args, parser, mode, options):
    present = []
    for name, flag in options.items():
        value = getattr(args, name)
        if value is not None and value is not False:
            present.append(flag)
    if present:
        parser.error(f"{mode} does not accept: {', '.join(sorted(present))}")


def _dispatch_hosted_mode(args, parser):
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
        _require_nonempty_option(parser, args.expect_bundle, "--expect-bundle")
        _require_nonempty_option(parser, args.expect_artifact, "--expect-artifact")
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
        _require_nonempty_option(parser, args.expect_inventory, "--expect-inventory")
        _require_nonempty_option(parser, args.export_replay, "--export-replay")
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
            parser.error("--doctor-mode/--expect-inventory require a doctor v3 profile")
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
