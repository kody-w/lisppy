#!/usr/bin/env python3
"""lispy_host: the thin I/O host that runs brainstem.lispy (LisPy, lispy-core@1) as a RAPP Brainstem.

The kernel logic lives in brainstem.lispy (LisPy). This file only does what a Lisp cannot do purely:
listen on $PORT, exchange the GitHub token for a Copilot token, POST to the model, discover and run
the user's Python agents (agents/*_agent.py stay Python: that is the universal agent format), and read
soul.md. Honors the rapp-1 conformance `cmd:` contract: $PORT, $FAKE_TOKEN_URL (or $COPILOT_TOKEN_URL),
$AGENTS_PATH, $GITHUB_MODEL, $GITHUB_TOKEN, $SOUL_PATH.
  python3 lispy_host.py            # serves on $PORT (default 7071)
"""
import glob
import importlib.util
import inspect
import json
import os
import sys
import time
import urllib.error
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
LISPPY_DIR = Path(os.environ.get("LISPPY_DIR", HERE / "lisppy")).expanduser().resolve()
LISPPY_SHA256 = "2a6be9272cd49991a6e13929b923afd78e2ec62d35843a509b1d28046ca43d2c"  # lisp.py @ kody-w/lisppy v0.25.0 (lock-in)
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


def model_call(messages, tools):
    s = copilot_session()
    body = {"model": MODEL, "messages": messages}
    if tools:
        body["tools"] = tools
        body["tool_choice"] = "auto"
    req = urllib.request.Request(s["api"] + "/chat/completions", data=json.dumps(body).encode(),
                                 headers={"Authorization": f"Bearer {s['token']}", "Content-Type": "application/json",
                                          "Copilot-Integration-Id": "vscode-chat", "Editor-Version": "vscode/1.95.0"})
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"Model '{MODEL}' returned {e.code}. {e.read()[:300].decode(errors='replace')}")


# ── agents (the grail's load_agents, unchanged semantics: sorted files, every BasicAgent subclass) ──
def load_agents():
    agents = {}
    parent = str(Path(AGENTS_PATH).resolve().parent)
    if parent not in sys.path:
        sys.path.insert(0, parent)
    try:
        from agents.basic_agent import BasicAgent
    except Exception:
        return agents
    for f in sorted(glob.glob(os.path.join(AGENTS_PATH, "*_agent.py"))):
        if os.path.basename(f) == "basic_agent.py":
            continue
        try:
            spec = importlib.util.spec_from_file_location("agent_" + Path(f).stem, f)
            m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
            for _, cls in inspect.getmembers(m, inspect.isclass):
                if issubclass(cls, BasicAgent) and cls is not BasicAgent and cls.__module__ == m.__name__:
                    inst = cls()
                    agents[inst.name] = inst
        except Exception as e:
            print(f"[lispy] agent {f} skipped: {e}", flush=True)
    return agents


# ── the host bindings brainstem.lispy may use, on top of the lispy-core@1 default profile (never trusted) ──
def _j(v):
    """lisp value -> plain JSON python (lists may be Pairs)."""
    return L.lisp_to_json(v)


def make_env(agents):
    env = L.make_global_env(trusted=False, limits=L.ExecutionLimits(max_steps=None, max_call_depth=4096),
                            output=lambda text: None)

    def json_parse_or_nil(text):
        try:
            return L.json_to_lisp(json.loads(text))
        except (TypeError, ValueError):
            return L.NIL

    def model_call_safe(messages, tools):
        try:
            return L.json_to_lisp(model_call(_j(messages), _j(tools) or None))
        except Exception as e:
            print(f"[lispy] Final tool-less completion failed: {e}", flush=True)
            return L.NIL

    def agent_perform(name, args):
        try:
            return ["ok", str(agents[name].perform(**_j(args)))]
        except Exception as e:
            return ["error", str(e)]

    env["json-parse-or-nil"] = json_parse_or_nil
    env["model-call"] = lambda messages, tools: L.json_to_lisp(model_call(_j(messages), _j(tools) or None))
    env["model-call-safe"] = model_call_safe
    env["agents-load"] = lambda: list(agents.keys())
    env["agents-tools"] = lambda: L.json_to_lisp([a.to_tool() for a in agents.values()])
    env["agent-exists?"] = lambda name: name in agents
    env["agent-perform"] = agent_perform
    env["soul"] = lambda: Path(SOUL_PATH).read_text() if Path(SOUL_PATH).is_file() else ""
    env["soul-path"] = lambda: SOUL_PATH if Path(SOUL_PATH).is_file() else "missing"
    env["model"] = lambda: MODEL
    env["version"] = lambda: VERSION
    env["voice-mode"] = lambda: VOICE_MODE
    env["uuid"] = lambda: str(uuid.uuid4())
    return env


KERNEL_SOURCE = (HERE / "brainstem.lispy").read_text()


def run(form, agents, request=None):
    """Evaluate one call in a fresh environment holding the kernel (agents reload every request, like the grail)."""
    env = make_env(agents)
    L.run_string(KERNEL_SOURCE, env, source_name="brainstem.lispy")
    env["*request*"] = L.json_to_lisp(request)
    code, body = _j(L.run_string(form, env))
    return code, body


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
        if path != "/chat":
            return self.send_json(404, {"error": "not found"})
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n)
        try:
            data = json.loads(raw)
        except ValueError:
            data = None
        try:
            code, body = run("(handle-chat *request*)", load_agents(), data)
        except Exception as e:
            code, body = 500, {"error": str(e)}
        self.send_json(code, body)


if __name__ == "__main__":
    print(f"[lispy] RAPP Brainstem (LisPy kernel) v{VERSION} on http://127.0.0.1:{PORT}  model={MODEL}", flush=True)
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
