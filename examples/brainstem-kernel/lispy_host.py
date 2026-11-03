#!/usr/bin/env python3
"""lispy_host: the thin I/O host that runs brainstem.lispy (LisPy, lispy-core@1) as a RAPP Brainstem.

The kernel logic lives in brainstem.lispy (LisPy). This file only does what a Lisp cannot do purely:
listen on $PORT, exchange the GitHub token for a Copilot token, POST to the model, discover and run
the user's Python agents (agents/*_agent.py stay Python: that is the universal agent format), and read
soul.md. Honors the rapp-1 conformance `cmd:` contract: $PORT, $FAKE_TOKEN_URL (or $COPILOT_TOKEN_URL),
$AGENTS_PATH, $GITHUB_MODEL, $GITHUB_TOKEN, $SOUL_PATH.
Imports and kernel definitions are cached; request environments and agent instances are not.
POST /chat/stream relays upstream token deltas, with whole-reply fallback if streaming is refused.
Independent agent instances execute in parallel; calls sharing an instance retain input order.
  python3 lispy_host.py            # serves on $PORT (default 7071)
"""
import importlib.abc
import importlib.util
import inspect
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import types
import urllib.error
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
LISPPY_DIR = Path(os.environ.get("LISPPY_DIR", HERE / "lisppy")).expanduser().resolve()
LISPPY_SHA256 = "5bf8de8fac8168e7850f545f9217da33a632e34221dd966fa614d92cc75f5df2"  # lisp.py @ kody-w/lisppy v0.25.0 (lock-in)
import hashlib  # noqa: E402
_lisp_bytes = (LISPPY_DIR / "lisp.py").read_bytes()
if hashlib.sha256(_lisp_bytes).hexdigest() != LISPPY_SHA256:
    sys.exit(f"[lispy] refusing to run: {LISPPY_DIR}/lisp.py is not the pinned LisPy reference implementation")
sys.path.insert(0, str(LISPPY_DIR))
import lisp as L  # noqa: E402  (lispy-core@1 reference implementation)

VERSION = (HERE / "VERSION").read_text().strip() if (HERE / "VERSION").is_file() else "0.1.0"
PORT = int(os.environ.get("PORT", "7071"))
MODEL = os.environ.get("GITHUB_MODEL", "gpt-4o")
AGENTS_PATH = os.environ.get("AGENTS_PATH", str(HERE / "agents"))
SOUL_PATH = os.environ.get("SOUL_PATH", str(HERE / "soul.md"))
TOKEN_URL = os.environ.get("FAKE_TOKEN_URL") or os.environ.get("COPILOT_TOKEN_URL") or \
    "https://api.github.com/copilot_internal/v2/token"
VOICE_MODE = os.environ.get("VOICE_MODE", "false").lower() == "true"
_session = {"token": None, "expires": 0, "api": None}


# ── auth + model (what the grail's get_copilot_token / call_copilot do) ──
def _github_token():
    t = os.environ.get("GITHUB_TOKEN")
    if t:
        return t
    p = Path(os.environ.get("COPILOT_TOKEN_FILE") or Path.home() / ".brainstem" / "src" / "rapp_brainstem" / ".copilot_token")
    if not p.is_file():
        return None
    raw = p.read_text().strip()  # the grail stores {"access_token": ...} from the device-code flow
    try:
        return json.loads(raw).get("access_token")
    except ValueError:
        return raw


def copilot_session():
    if _session["token"] and time.time() < _session["expires"] - 60:
        return _session
    gh = _github_token()
    if not gh:
        raise RuntimeError("No GitHub token. Sign in with GitHub to use the Brainstem.")
    prefix = "token" if gh.startswith("ghu_") else "Bearer"  # the grail's own rule
    req = urllib.request.Request(TOKEN_URL, headers={
        "Authorization": f"{prefix} {gh}", "Accept": "application/json", "Editor-Version": "vscode/1.95.0",
        "Editor-Plugin-Version": "copilot/1.0.0", "User-Agent": "GitHubCopilotChat/0.22.2024"})
    with urllib.request.urlopen(req, timeout=20) as r:
        d = json.loads(r.read())
    _session.update(token=d["token"], expires=d.get("expires_at", time.time() + 1500),
                    api=(d.get("endpoints") or {}).get("api") or "https://api.githubcopilot.com")
    return _session


def _read_model_stream(response, emit):
    """Assemble choice zero while forwarding each upstream content delta immediately."""
    message = {"role": "assistant", "content": None}
    content = []
    calls = {}
    model = MODEL
    finish = None
    completed = False
    data = []

    def consume(payload):
        nonlocal model, finish, completed
        if payload == "[DONE]":
            completed = True
            return
        chunk = json.loads(payload)
        if chunk.get("error"):
            raise RuntimeError(f"Model stream error: {chunk['error']}")
        model = chunk.get("model") or model
        for choice in chunk.get("choices", []):
            if choice.get("index", 0) != 0:
                continue
            delta = choice.get("delta") or {}
            if delta.get("role"):
                message["role"] = delta["role"]
            text = delta.get("content")
            if text:
                content.append(text)
                emit({"type": "delta", "text": text})
            for part in delta.get("tool_calls") or []:
                index = part["index"]
                call = calls.setdefault(index, {
                    "id": "", "type": "function", "function": {"name": "", "arguments": ""}})
                if part.get("id"):
                    call["id"] += part["id"]
                if part.get("type"):
                    call["type"] = part["type"]
                function = part.get("function") or {}
                for key in ("name", "arguments"):
                    if function.get(key):
                        call["function"][key] += function[key]
            if choice.get("finish_reason") is not None:
                finish = choice["finish_reason"]

    for raw in response:
        line = raw.decode("utf-8").rstrip("\r\n")
        if not line:
            if data:
                consume("\n".join(data))
                data.clear()
                if completed:
                    break
        elif line.startswith("data:"):
            value = line[5:]
            data.append(value[1:] if value.startswith(" ") else value)
    if data:
        consume("\n".join(data))
    if finish is None:
        raise RuntimeError("Model stream interrupted before completion")
    if content:
        message["content"] = "".join(content)
    if calls:
        message["tool_calls"] = [calls[index] for index in sorted(calls)]
    return {"model": model, "choices": [
        {"index": 0, "message": message, "finish_reason": finish}]}


def model_call(messages, tools, emit=None):
    s = copilot_session()
    body = {"model": MODEL, "messages": messages}
    if tools:
        body["tools"] = tools
        body["tool_choice"] = "auto"
    streaming = emit is not None
    for attempt in range(2 if streaming else 1):
        if streaming:
            body["stream"] = True
        else:
            body.pop("stream", None)
        req = urllib.request.Request(
            s["api"] + "/chat/completions", data=json.dumps(body).encode(),
            headers={"Authorization": f"Bearer {s['token']}", "Content-Type": "application/json",
                     "Accept": "text/event-stream" if streaming else "application/json",
                     "Copilot-Integration-Id": "vscode-chat", "Editor-Version": "vscode/1.95.0"})
        try:
            with urllib.request.urlopen(req, timeout=120) as response:
                if streaming and response.headers.get_content_type() == "text/event-stream":
                    return _read_model_stream(response, emit)
                # Some endpoints ignore stream=true and return a complete JSON reply.
                return json.loads(response.read())
        except urllib.error.HTTPError as error:
            detail = error.read()[:300].decode(errors="replace")
            error.close()
            if streaming and error.code in (400, 404, 405, 415, 422, 501):
                print(f"[lispy] model refused streaming ({error.code}); retrying non-streaming", flush=True)
                streaming = False
                continue
            raise RuntimeError(f"Model '{MODEL}' returned {error.code}. {detail}") from error


# ── local storage, exposed through the agents' usual cloud-storage imports ──
_DATA_DIR = HERE / ".brainstem_data"
_storage_locks = {}
_storage_guard = threading.Lock()


def _storage_lock(path):
    with _storage_guard:
        return _storage_locks.setdefault(str(path), threading.RLock())


class AzureFileStorageManager:
    """Local, atomic JSON storage with the grail's shared/user/share layout."""

    DEFAULT_MARKER_GUID = "c0p110t0-aaaa-bbbb-cccc-123456789abc"

    def __init__(self, share_name=None, **kwargs):
        self._context = threading.local()
        self.current_guid = None
        self.share_name = str(share_name or "").strip().lower() or None
        self.storage_root = (
            os.path.join("shares", hashlib.sha256(self.share_name.encode()).hexdigest())
            if self.share_name else ""
        )
        self.shared_memory_path = os.path.join(self.storage_root, "shared_memories")
        self.default_file_name = "memory.json"
        self.current_memory_path = self.shared_memory_path

    @property
    def current_guid(self):
        return getattr(self._context, "guid", None)

    @current_guid.setter
    def current_guid(self, value):
        self._context.guid = value

    @property
    def current_memory_path(self):
        return getattr(self._context, "path", self.shared_memory_path)

    @current_memory_path.setter
    def current_memory_path(self, value):
        self._context.path = value

    def set_memory_context(self, user_guid=None):
        if user_guid is None or user_guid == "" or user_guid == self.DEFAULT_MARKER_GUID:
            self.current_guid = None
            self.current_memory_path = self.shared_memory_path
        else:
            reserved = {"CON", "PRN", "AUX", "NUL"}
            reserved.update(f"{prefix}{n}" for prefix in ("COM", "LPT") for n in range(1, 10))
            if (not isinstance(user_guid, str) or user_guid in {".", ".."}
                    or user_guid.endswith((".", " "))
                    or any(c in '<>:"/\\|?*' or ord(c) < 32 for c in user_guid)
                    or user_guid.split(".", 1)[0].upper() in reserved):
                raise ValueError("user_guid must be a single path component")
            self.current_guid = user_guid
            self.current_memory_path = os.path.join(self.storage_root, "memory", user_guid)
        return True

    def _scoped_path(self, file_path=""):
        root = (_DATA_DIR / self.storage_root).resolve()
        target = (root / file_path).resolve()
        if not target.is_relative_to(root):
            raise ValueError(f"path escapes data directory: {file_path}")
        return target

    def _file_path(self):
        if self.current_guid:
            self.set_memory_context(self.current_guid)
            return self._scoped_path(os.path.join("memory", self.current_guid, "user_memory.json"))
        return self._scoped_path(os.path.join("shared_memories", self.default_file_name))

    def _write(self, path, content):
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def read_json(self, file_path=None):
        path = self._scoped_path(file_path) if file_path else self._file_path()
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except (ValueError, OSError) as error:
            print(f"[lispy] storage read {path} failed: {error}", flush=True)
            return {}

    def write_json(self, data, file_path=None):
        path = self._scoped_path(file_path) if file_path else self._file_path()
        with _storage_lock(path):
            self._write(path, json.dumps(data, indent=2, default=str))
        return True

    def update_json(self, update_fn, file_path=None):
        path = self._scoped_path(file_path) if file_path else self._file_path()
        with _storage_lock(path):
            try:
                current = json.loads(path.read_text(encoding="utf-8"))
            except FileNotFoundError:
                current = {}
            updated = update_fn(current)
            self._write(path, json.dumps(updated, indent=2, default=str))
            return updated

    def read_file(self, file_path):
        try:
            return self._scoped_path(file_path).read_text(encoding="utf-8")
        except FileNotFoundError:
            return None

    def write_file(self, file_path, content):
        path = self._scoped_path(file_path)
        with _storage_lock(path):
            self._write(path, content)
        return True

    def list_files(self, directory=""):
        path = self._scoped_path(directory)
        return os.listdir(path) if path.exists() else []

    def delete_file(self, file_path):
        try:
            self._scoped_path(file_path).unlink()
            return True
        except FileNotFoundError:
            return False

    def file_exists(self, file_path):
        try:
            return self._scoped_path(file_path).exists()
        except ValueError:
            return False


def _register_shims():
    if _agent_finder not in sys.meta_path:
        sys.meta_path.insert(0, _agent_finder)
    if "utils" not in sys.modules:
        package = types.ModuleType("utils")
        package.__path__ = []
        sys.modules["utils"] = package
    for name, attribute, value in (
        ("azure_file_storage", "AzureFileStorageManager", AzureFileStorageManager),
        ("dynamics_storage", "DynamicsStorageManager", AzureFileStorageManager),
        ("storage_factory", "get_storage_manager", lambda: AzureFileStorageManager()),
    ):
        module = types.ModuleType(f"utils.{name}")
        setattr(module, attribute, value)
        sys.modules[module.__name__] = module
        setattr(sys.modules["utils"], name, module)


# ── agents: sorted files, every BasicAgent subclass, reload on directory changes ──
_agents_lock = threading.RLock()
_agents_cache_key = None
_agents_cache = {}
_ensurepip_attempted = False
_install_attempts = set()
_PIP_MAP = {"bs4": "beautifulsoup4", "PIL": "Pillow", "cv2": "opencv-python",
            "sklearn": "scikit-learn", "yaml": "pyyaml", "docx": "python-docx",
            "pptx": "python-pptx", "dotenv": "python-dotenv"}


class _AgentSourceLoader(importlib.machinery.SourceFileLoader):
    def get_code(self, fullname):
        return compile(self.get_data(self.path), self.path, "exec", dont_inherit=True)


class _AgentSourceFinder(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if not fullname.startswith("agents.") or path is None:
            return None
        spec = importlib.machinery.PathFinder.find_spec(fullname, path, target)
        if spec is not None and spec.origin and spec.origin.endswith(".py"):
            spec.loader = _AgentSourceLoader(fullname, spec.origin)
        return spec


_agent_finder = _AgentSourceFinder()


def _auto_install(error):
    global _ensurepip_attempted
    if not error.name:
        return False
    module = error.name.split(".")[0]
    package = _PIP_MAP.get(module, module)
    if package in _install_attempts:
        return False
    _install_attempts.add(package)
    try:
        bootstrap = ""
        if not _ensurepip_attempted:
            _ensurepip_attempted = True
            result = subprocess.run([sys.executable, "-m", "ensurepip"],
                                    capture_output=True, text=True, timeout=60)
            bootstrap = f"ensurepip={result.returncode}; "
        result = subprocess.run([sys.executable, "-m", "pip", "install", package, "-q"],
                                capture_output=True, text=True, timeout=60)
        detail = "installed" if result.returncode == 0 else f"failed: {result.stderr[:200]}"
        print(f"[lispy] dependency {package}: {bootstrap}{detail}".replace("\n", " "), flush=True)
        if result.returncode == 0:
            importlib.invalidate_caches()
            return True
    except (OSError, subprocess.SubprocessError) as failure:
        print(f"[lispy] dependency {package} failed: {failure}".replace("\n", " "), flush=True)
    return False


def _import_agent(name, path):
    spec = importlib.util.spec_from_file_location(
        name, path, loader=_AgentSourceLoader(name, str(path)))
    module = importlib.util.module_from_spec(spec)
    # Source-only loading also covers agents importing one another via the package.
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
    except Exception:
        sys.modules.pop(name, None)
        raise
    parent, _, attribute = name.rpartition(".")
    if parent in sys.modules:
        setattr(sys.modules[parent], attribute, module)
    return module


def load_agents():
    global _agents_cache_key, _agents_cache
    with _agents_lock:
        directory = Path(AGENTS_PATH).expanduser().resolve()
        files = sorted(directory.glob("*_agent.py"))
        stamps = tuple((f.name, f.stat().st_mtime_ns, f.stat().st_size) for f in files)
        directory_time = directory.stat().st_mtime_ns if directory.exists() else 0
        key = (str(directory), max([directory_time, *(s[1] for s in stamps)]),
               directory_time, stamps)
        if key == _agents_cache_key:
            return _agents_cache
        _register_shims()
        for name in list(sys.modules):
            if name == "agents" or name.startswith("agents."):
                del sys.modules[name]
        package = types.ModuleType("agents")
        package.__path__ = [str(directory)]
        sys.modules["agents"] = package
        agents = {}
        try:
            basic = _import_agent("agents.basic_agent", directory / "basic_agent.py")
            package.basic_agent = basic
            BasicAgent = basic.BasicAgent
        except Exception as error:
            print(f"[lispy] BasicAgent {directory} unavailable: {error}", flush=True)
            _agents_cache_key, _agents_cache = key, agents
            return agents
        for path in files:
            if path.name == "basic_agent.py":
                continue
            for attempt in range(2):
                try:
                    name = "agents." + path.stem
                    module = sys.modules.get(name)
                    if module is None:
                        module = _import_agent(name, path)
                    for _, cls in inspect.getmembers(module, inspect.isclass):
                        if issubclass(cls, BasicAgent) and cls is not BasicAgent and cls.__module__ == module.__name__:
                            instance = cls()
                            agents[instance.name] = instance
                    break
                except ModuleNotFoundError as error:
                    if attempt == 0 and _auto_install(error):
                        continue
                    print(f"[lispy] agent {path} skipped: {error}", flush=True)
                    break
                except Exception as error:
                    print(f"[lispy] agent {path} skipped: {error}", flush=True)
                    break
        _agents_cache_key, _agents_cache = key, agents
        return agents


# ── the host bindings brainstem.lispy may use, on top of the lispy-core@1 default profile (never trusted) ──
def _j(v):
    """lisp value -> plain JSON python (lists may be Pairs)."""
    return L.lisp_to_json(v)


def make_env(agents, emit=None):
    env = L.make_global_env(trusted=False, load_stdlib=False,
                            limits=L.ExecutionLimits(max_steps=None, max_call_depth=4096),
                            output=lambda text: None)
    pending = callable(agents)
    answer_streamed = False
    streamed_parts = []

    def invoke_model(messages, tools):
        nonlocal answer_streamed
        answer_streamed = False
        streamed_parts.clear()

        def relay(event):
            nonlocal answer_streamed
            answer_streamed = True
            streamed_parts.append(event["text"])
            emit(event)

        args = (_j(messages), _j(tools) or None)
        response = model_call(*args, emit=relay) if emit else model_call(*args)
        return L.json_to_lisp(response)

    def emit_kernel(event):
        nonlocal answer_streamed
        if not emit:
            return L.NIL
        event = _j(event)
        if event.get("type") == "delta" and answer_streamed:
            text = "".join(streamed_parts)
            if event["text"] in (text, text.strip()):
                return L.NIL
            # The kernel can replace an empty/whitespace completion with its own fallback.
            answer_streamed = False
        if event.get("type") == "done":
            event["streamed"] = answer_streamed
        emit(event)
        return L.NIL

    def current_agents():
        nonlocal agents, pending
        if pending:
            agents = agents()
            pending = False
        return agents

    def json_parse_or_nil(text):
        try:
            return L.json_to_lisp(json.loads(text))
        except (TypeError, ValueError):
            return L.NIL

    def model_call_safe(messages, tools):
        try:
            return invoke_model(messages, tools)
        except Exception as e:
            if emit:
                raise
            print(f"[lispy] Final tool-less completion failed: {e}", flush=True)
            return L.NIL

    def agent_perform(name, args):
        try:
            return ["ok", str(current_agents()[name].perform(**_j(args)))]
        except Exception as e:
            return ["error", str(e)]

    def agents_perform_all(tasks):
        jobs = _j(tasks) or []
        available = current_agents()

        def perform(job):
            if not job:
                return None
            name, args = job
            try:
                return ["ok", str(available[name].perform(**args))]
            except Exception as error:
                return ["error", str(error)]

        if len(jobs) < 2:
            results = [perform(job) for job in jobs]
        else:
            groups = {}
            results = [None] * len(jobs)
            for index, job in enumerate(jobs):
                if job:
                    groups.setdefault(id(available[job[0]]), []).append((index, job))

            def perform_group(group):
                # Preserve sequential semantics for repeated calls on one mutable agent.
                return [(index, perform(job)) for index, job in group]

            with ThreadPoolExecutor(max_workers=min(32, max(1, len(groups)))) as pool:
                for completed in pool.map(perform_group, groups.values()):
                    for index, result in completed:
                        results[index] = result
        return L.json_to_lisp(results)

    env["json-parse-or-nil"] = json_parse_or_nil
    env["model-call"] = invoke_model
    env["model-call-safe"] = model_call_safe
    env["agents-load"] = lambda: list(current_agents().keys())
    env["agents-tools"] = lambda: L.json_to_lisp([a.to_tool() for a in current_agents().values()])
    env["agent-exists?"] = lambda name: name in current_agents()
    env["agent-perform"] = agent_perform
    env["agents-perform-all"] = agents_perform_all
    env["emit"] = emit_kernel
    env["soul"] = lambda: Path(SOUL_PATH).read_text() if Path(SOUL_PATH).is_file() else ""
    env["soul-path"] = lambda: SOUL_PATH if Path(SOUL_PATH).is_file() else "missing"
    env["model"] = lambda: MODEL
    env["version"] = lambda: VERSION
    env["voice-mode"] = lambda: VOICE_MODE
    env["uuid"] = lambda: str(uuid.uuid4())
    return env


KERNEL_SOURCE = (HERE / "brainstem.lispy").read_text()
_kernel_lock = threading.Lock()
_kernel_identity = None
_kernel_base = None


def _bind_kernel(env):
    global _kernel_identity, _kernel_base
    identity = hashlib.sha256(KERNEL_SOURCE.encode()).digest()
    with _kernel_lock:
        if identity != _kernel_identity:
            base = L.make_global_env(
                trusted=False, limits=L.ExecutionLimits(max_steps=None, max_call_depth=4096),
                output=lambda text: None)
            existing = set(base)
            L.run_string(KERNEL_SOURCE, base, source_name="brainstem.lispy")
            if not all(isinstance(base[name], L.Lambda) for name in set(base) - existing):
                raise RuntimeError("Kernel top-level definitions must be procedures")
            _kernel_base = {name: value for name, value in base.items()
                            if isinstance(value, (L.Lambda, L.Macro))}
            _kernel_identity = identity
        definitions = tuple(_kernel_base.items())
    # Lexical closures must point at THIS request, not the cached base or another request.
    for name, value in definitions:
        env[name] = type(value)(value.params, value.body, env, name=value.name)


def run(form, agents, request=None, emit=None):
    """Reuse kernel definitions, with fresh lexical bindings and execution limits per request."""
    env = make_env(agents, emit)
    _bind_kernel(env)
    env["*request*"] = L.json_to_lisp(request)
    code, body = _j(L.run_string(form, env))
    return code, body


def _request_agents():
    # Cache imports/discovery, not mutable per-conversation agent state.
    return {name: type(agent)() for name, agent in load_agents().items()}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def send_json(self, code, body):
        raw = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/health":
            code, body = run("(handle-health)", load_agents())
        elif path == "/version":
            code, body = run("(handle-version)", {})
        else:
            return self.send_json(404, {"error": "not found"})
        self.send_json(code, body)

    def do_POST(self):
        path = self.path.split("?")[0]
        if path not in ("/chat", "/chat/stream"):
            return self.send_json(404, {"error": "not found"})
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n)
        try:
            data = json.loads(raw)
        except ValueError:
            data = None
        streaming = path == "/chat/stream"
        started = False

        def emit(event):
            nonlocal started
            if not started:
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("X-Accel-Buffering", "no")
                self.send_header("Connection", "close")
                self.end_headers()
                self.close_connection = True
                started = True
            self.wfile.write(f"data: {json.dumps(event)}\n\n".encode())
            self.wfile.flush()

        try:
            form = "(handle-chat-stream *request*)" if streaming else "(handle-chat *request*)"
            code, body = run(form, _request_agents, data, emit=emit if streaming else None)
        except (BrokenPipeError, ConnectionResetError):
            return
        except Exception as e:
            if started:
                emit({"type": "error", "error": str(e)})
                return
            code, body = 500, {"error": str(e)}
        if not started:
            self.send_json(code, body)


if __name__ == "__main__":
    print(f"[lispy] RAPP Brainstem (LisPy kernel) v{VERSION} on http://127.0.0.1:{PORT}  model={MODEL}", flush=True)
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
