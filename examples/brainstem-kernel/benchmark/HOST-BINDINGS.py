# ── the host bindings brainstem.lisp may use, on top of the lispy-core@1 default profile (never trusted) ──
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


KERNEL_SOURCE = (HERE / "brainstem.lisp").read_text()
