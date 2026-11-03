#!/usr/bin/env python3
"""Host regression checks. Run with python3 tests_host.py (stdlib unittest only)."""
import concurrent.futures
import contextlib
from email.message import Message
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import types
import unittest
from unittest import mock
import urllib.error
import urllib.request

import bench
import lispy_host as host

GRAIL = Path(os.environ.get(
    "GRAIL_DIR", host.HERE.parent / "grail-pin-0616" / "rapp_brainstem")).expanduser()


class AgentTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name).resolve() / "agents"
        self.directory.mkdir()
        shutil.copy2(GRAIL / "agents/basic_agent.py", self.directory / "basic_agent.py")
        for name, value in (("AGENTS_PATH", str(self.directory)),
                            ("_agents_cache_key", None), ("_agents_cache", {}),
                            ("_ensurepip_attempted", False), ("_install_attempts", set())):
            patch = mock.patch.object(host, name, value)
            patch.start()
            self.addCleanup(patch.stop)
        self.output = self.enterContext(contextlib.redirect_stdout(io.StringIO()))

    def agent(self, filename="echo_agent.py", name="Echo", reply="first"):
        path = self.directory / filename
        path.write_text(
            "from agents.basic_agent import BasicAgent\n"
            "class Example(BasicAgent):\n"
            f"    def __init__(self): self.name = {name!r}\n"
            "    def to_tool(self): return {}\n"
            f"    def perform(self, **kwargs): return {reply!r}\n")
        return path

    def test_reload_on_same_size_edit_and_reuse_unchanged(self):
        path = self.agent()
        first = host.load_agents()
        self.assertIs(first, host.load_agents())
        self.assertEqual(first["Echo"].perform(), "first")
        self.agent(reply="other")
        os.utime(path, ns=(path.stat().st_atime_ns, path.stat().st_mtime_ns + 1_000_000))
        second = host.load_agents()
        self.assertIsNot(first, second)
        self.assertEqual(second["Echo"].perform(), "other")
        self.assertEqual(first["Echo"].perform(), "first")

    def test_edit_non_newest_file_add_delete_and_path_switch(self):
        self.agent()
        future = self.agent("future_agent.py", "Future")
        stamp = future.stat().st_mtime_ns + 10_000_000_000
        os.utime(future, ns=(stamp, stamp))
        host.load_agents()
        self.agent(reply="other")
        self.assertEqual(host.load_agents()["Echo"].perform(), "other")
        self.agent("added_agent.py", "Added")
        self.assertIn("Added", host.load_agents())
        future.unlink()
        self.assertNotIn("Future", host.load_agents())
        other = self.directory.parent / "other"
        other.mkdir()
        shutil.copy2(self.directory / "basic_agent.py", other / "basic_agent.py")
        shutil.copy2(self.directory / "echo_agent.py", other / "echo_agent.py")
        with mock.patch.object(host, "AGENTS_PATH", str(other)):
            self.assertEqual(set(host.load_agents()), {"Echo"})
        self.assertIn("Added", host.load_agents())

    def test_concurrent_reload_only_imports_once(self):
        self.agent()
        with mock.patch.object(host, "_import_agent", wraps=host._import_agent) as imported:
            with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
                results = list(pool.map(lambda _: host.load_agents(), range(16)))
        self.assertTrue(all(result is results[0] for result in results))
        self.assertEqual(imported.call_count, 2)

    def test_missing_basic_agent_is_cached_until_repaired(self):
        basic = self.directory / "basic_agent.py"
        source = basic.read_bytes()
        basic.unlink()
        with mock.patch.object(host, "_import_agent", wraps=host._import_agent) as imported:
            self.assertEqual(host.load_agents(), {})
            self.assertEqual(host.load_agents(), {})
            self.assertEqual(imported.call_count, 1)
            basic.write_bytes(source)
            self.agent()
            self.assertIn("Echo", host.load_agents())
        self.assertIn("unavailable", self.output.getvalue())

    def test_request_instances_do_not_share_mutable_state(self):
        self.agent()
        first = host._request_agents()
        second = host._request_agents()
        self.assertIsNot(first["Echo"], second["Echo"])
        first["Echo"].conversation = "private"
        self.assertFalse(hasattr(second["Echo"], "conversation"))

    def test_next_http_chat_request_observes_agent_edit(self):
        self.agent()

        def model(messages, tools, emit=None):
            message = ({"role": "assistant", "content": messages[-1]["content"]}
                       if messages[-1]["role"] == "tool" else {
                           "role": "assistant", "content": None, "tool_calls": [{
                               "id": "test", "function": {"name": "Echo", "arguments": "{}"}}]})
            return {"model": "test", "choices": [{"message": message}]}

        with host.ThreadingHTTPServer(("127.0.0.1", 0), host.Handler) as server:
            thread = threading.Thread(target=server.serve_forever)
            thread.start()
            try:
                with mock.patch.object(host, "model_call", side_effect=model):
                    for expected in ("first", "other"):
                        self.agent(reply=expected)
                        request = urllib.request.Request(
                            f"http://127.0.0.1:{server.server_port}/chat",
                            data=b'{"user_input":"run tool","session_id":"fixed"}',
                            headers={"Content-Type": "application/json"})
                        with urllib.request.urlopen(request, timeout=5) as response:
                            self.assertEqual(json.load(response)["response"], expected)
            finally:
                server.shutdown()
                thread.join(timeout=5)

    def test_indirect_agent_import_ignores_stale_bytecode_and_shares_module(self):
        self.agent(filename="a_agent.py", name="A")
        importer = self.directory / "a_agent.py"
        importer.write_text(
            "from agents.basic_agent import BasicAgent\n"
            "from agents.z_agent import VALUE\n"
            "class Example(BasicAgent):\n"
            "    def __init__(self): self.name = 'A'\n"
            "    def perform(self, **kwargs): return VALUE\n")
        dependency = self.directory / "z_agent.py"
        dependency.write_text('VALUE = "old"\n')
        os.utime(dependency, ns=(100_100_000_000, 100_100_000_000))
        # A real timestamp-based pyc left by an earlier host/importer.
        import py_compile
        py_compile.compile(str(dependency), doraise=True)
        first = host.load_agents()
        self.assertEqual(first["A"].perform(), "old")
        dependency.write_text('VALUE = "new"\n')
        os.utime(dependency, ns=(100_900_000_000, 100_900_000_000))
        second = host.load_agents()
        self.assertEqual(second["A"].perform(), "new")
        self.assertEqual(sys.modules["agents.z_agent"].VALUE, "new")
        self.assertIs(sys.modules["agents"].z_agent, sys.modules["agents.z_agent"])

    def test_utils_shim_loads_real_memory_agents_and_roundtrips(self):
        for name in ("context_memory_agent.py", "manage_memory_agent.py"):
            shutil.copy2(GRAIL / "agents" / name, self.directory / name)
        with mock.patch.object(host, "_DATA_DIR", self.directory.parent / "data"):
            agents = host.load_agents()
            self.assertEqual(set(agents), {"ContextMemory", "ManageMemory"})
            self.assertIs(sys.modules["utils"].azure_file_storage.AzureFileStorageManager,
                          host.AzureFileStorageManager)
            saved = agents["ManageMemory"].perform(
                memory_type="fact", content="host test sentinel", user_guid="test-user")
            self.assertNotIn("Error", saved)
            recalled = agents["ContextMemory"].perform(user_guid="test-user")
            self.assertIn("host test sentinel", recalled)
            self.assertNotIn("host test sentinel", agents["ContextMemory"].perform())
            self.assertTrue((host._DATA_DIR / "memory/test-user/user_memory.json").is_file())

    def test_dependency_bootstrap_once_install_once_and_import_retry(self):
        path = self.agent()
        original = host._import_agent
        attempts = 0

        def importing(name, source):
            nonlocal attempts
            if source == path:
                attempts += 1
                if attempts == 1:
                    raise ModuleNotFoundError("No module named 'yaml'", name="yaml")
            return original(name, source)

        success = subprocess.CompletedProcess([], 0, "", "")
        with mock.patch.object(host, "_import_agent", side_effect=importing), \
                mock.patch.object(host.subprocess, "run", return_value=success) as run:
            self.assertIn("Echo", host.load_agents())
            self.assertEqual(run.call_args_list[0].args[0], [sys.executable, "-m", "ensurepip"])
            self.assertEqual(run.call_args_list[1].args[0],
                             [sys.executable, "-m", "pip", "install", "pyyaml", "-q"])
            self.assertFalse(host._auto_install(ModuleNotFoundError(name="yaml")))
            self.assertTrue(host._auto_install(ModuleNotFoundError(name="another_dependency")))
            self.assertEqual(run.call_count, 3)
        self.assertEqual(attempts, 2)
        self.assertEqual(len([s for s in self.output.getvalue().splitlines() if "dependency " in s]), 2)

    def test_failed_install_and_repeated_missing_import_never_loop(self):
        path = self.agent()
        original = host._import_agent

        def importing(name, source):
            if source == path:
                raise ModuleNotFoundError("No module named 'not_real'", name="not_real")
            return original(name, source)

        failed = subprocess.CompletedProcess([], 1, "", "failure")
        with mock.patch.object(host, "_import_agent", side_effect=importing), \
                mock.patch.object(host.subprocess, "run", return_value=failed) as run:
            self.assertEqual(host.load_agents(), {})
            self.agent(reply="other")
            self.assertEqual(host.load_agents(), {})
            self.assertEqual(run.call_count, 2)
        self.assertIn("failed: failure", self.output.getvalue())

    def test_successful_install_still_missing_stops_after_one_retry(self):
        self.agent()
        basic = host._import_agent("test_basic", self.directory / "basic_agent.py")
        missing = ModuleNotFoundError("No module named 'missing'", name="missing")
        with mock.patch.object(host, "_import_agent", side_effect=[basic, missing, missing]) as imported, \
                mock.patch.object(host.subprocess, "run",
                                  return_value=subprocess.CompletedProcess([], 0, "", "")) as run:
            self.assertEqual(host.load_agents(), {})
        self.assertEqual(imported.call_count, 3)
        self.assertEqual(run.call_count, 2)


class StorageTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        patch = mock.patch.object(host, "_DATA_DIR", self.root / "data")
        patch.start()
        self.addCleanup(patch.stop)
        self.storage = host.AzureFileStorageManager()

    def test_layout_shares_context_and_file_helpers(self):
        store = self.storage
        self.assertEqual(store.read_json(), {})
        store.write_json({"shared": 1})
        self.assertTrue((host._DATA_DIR / "shared_memories/memory.json").exists())
        store.set_memory_context("user")
        store.write_json({"private": 2})
        self.assertTrue((host._DATA_DIR / "memory/user/user_memory.json").exists())
        store.set_memory_context(store.DEFAULT_MARKER_GUID)
        self.assertEqual(store.read_json(), {"shared": 1})
        named = host.AzureFileStorageManager(share_name=" DEMO ")
        named.write_json({"named": 3})
        self.assertEqual(host.AzureFileStorageManager(share_name="demo").read_json(), {"named": 3})
        self.assertEqual(host.AzureFileStorageManager(share_name="other").read_json(), {})
        self.assertTrue(named.write_file("folder/a.txt", "hello"))
        self.assertEqual(named.read_file("folder/a.txt"), "hello")
        self.assertEqual(named.list_files("folder"), ["a.txt"])
        self.assertTrue(named.file_exists("folder/a.txt"))
        self.assertTrue(named.delete_file("folder/a.txt"))
        self.assertFalse(named.delete_file("folder/a.txt"))
        self.assertIsNone(named.read_file("folder/a.txt"))
        self.assertEqual(named.list_files("missing"), [])

    def test_containment_and_invalid_context(self):
        for context in ("../escape", "/absolute", "a\\b", ".", "..", "NUL", "a.", "a ", 123):
            with self.subTest(context=context), self.assertRaises(ValueError):
                self.storage.set_memory_context(context)
        for path in ("../escape", str(self.root / "outside")):
            with self.subTest(path=path), self.assertRaises(ValueError):
                self.storage.write_json({}, path)
            self.assertFalse(self.storage.file_exists(path))
        host._DATA_DIR.mkdir()
        (host._DATA_DIR / "link").symlink_to(self.root, target_is_directory=True)
        with self.assertRaises(ValueError):
            self.storage.write_file("link/outside", "no")

    def test_atomic_updates_multiple_managers_and_corruption_preserved(self):
        def increment(_):
            return host.AzureFileStorageManager().update_json(
                lambda data: {"count": data.get("count", 0) + 1})
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(increment, range(40)))
        self.assertEqual(self.storage.read_json(), {"count": 40})
        path = self.storage._file_path()
        path.write_text("corrupt")
        with self.assertRaises(ValueError):
            self.storage.update_json(lambda _: {})
        self.assertEqual(path.read_text(), "corrupt")
        self.assertEqual(list(path.parent.glob("*.tmp")), [])
        if os.name == "posix":
            self.storage.write_json({})
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_parallel_memory_contexts_cannot_cross_users(self):
        barrier = threading.Barrier(2)

        def remember(user):
            self.storage.set_memory_context(user)
            barrier.wait(timeout=2)
            self.storage.write_json({"user": user})
            return self.storage.read_json()

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(remember, ("alice", "bob")))
        self.assertEqual(results, [{"user": "alice"}, {"user": "bob"}])
        for user in ("alice", "bob"):
            self.storage.set_memory_context(user)
            self.assertEqual(self.storage.read_json(), {"user": user})


class KernelTests(unittest.TestCase):
    def test_kernel_evaluated_once_per_identity(self):
        with mock.patch.object(host, "_kernel_identity", None), \
                mock.patch.object(host.L, "run_string", wraps=host.L.run_string) as run:
            for _ in range(3):
                self.assertEqual(host.run("(handle-health)", {})[0], 200)
            self.assertEqual(sum(call.kwargs.get("source_name") == "brainstem.lispy"
                                 for call in run.call_args_list), 1)
            with mock.patch.object(host, "KERNEL_SOURCE", host.KERNEL_SOURCE + "\n;; new identity\n"):
                host.run("(handle-health)", {})
            self.assertEqual(sum(call.kwargs.get("source_name") == "brainstem.lispy"
                                 for call in run.call_args_list), 2)

    def test_concurrent_requests_have_isolated_closures_and_emitters(self):
        barrier = threading.Barrier(6)

        def request(index):
            events = []
            barrier.wait(timeout=5)
            with_agents = {f"Agent{index}": object()}
            code, body = host.run("(handle-health)", with_agents)
            return index, code, body, events

        with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
            results = list(pool.map(request, range(6)))
        for index, code, body, _ in results:
            self.assertEqual(code, 200)
            self.assertEqual(body["agents"], [f"Agent{index}"])

    def test_cached_stdlib_procedures_and_macros_are_request_local(self):
        env = host.make_env({})
        host._bind_kernel(env)
        self.assertIs(env["identity"].env, env)
        self.assertIs(env["when"].env, env)
        self.assertEqual(host.L.run_string("(when #t (identity 42))", env), 42)

    def tool_results(self, agents, calls):
        env = host.make_env(agents)
        host._bind_kernel(env)
        env["calls"] = host.L.json_to_lisp(calls)
        return host._j(host.L.run_string("(run-tool-calls calls)", env))

    def test_parallel_matches_sequential_including_errors_and_preserves_order(self):
        class Echo:
            def perform(self, **kwargs):
                return kwargs.get("text", "empty")

        class Boom:
            def perform(self, **kwargs):
                raise ValueError("it blew up")

        calls = [
            {"id": str(i), "function": {"name": name, "arguments": args}}
            for i, (name, args) in enumerate((
                ("Echo", '{"text":"first"}'), ("Boom", "{}"), ("Nobody", "{}"),
                ("Echo", "[]"), ("Echo", "bad json"), ("Echo", '{"text":"last"}')))
        ]
        agents = {"Echo": Echo(), "Boom": Boom()}
        parallel = self.tool_results(agents, calls)

        class Sequential:
            def __init__(self, **kwargs):
                pass
            def __enter__(self):
                return self
            def __exit__(self, *args):
                pass
            def map(self, fn, jobs):
                return map(fn, jobs)

        with mock.patch.object(host, "ThreadPoolExecutor", Sequential):
            sequential = self.tool_results(agents, calls)
        self.assertEqual(parallel, sequential)
        messages, logs = parallel
        self.assertEqual([m["tool_call_id"] for m in messages], [str(i) for i in range(6)])
        self.assertEqual([m["content"] for m in messages], [
            "first", "Error: it blew up", "Agent 'Nobody' not found.",
            "Error: Tool arguments must be a valid JSON object.",
            "Error: Tool arguments must be a valid JSON object.", "last"])
        self.assertEqual(logs[1], "[Boom] ERROR: it blew up")
        self.assertEqual(self.tool_results(agents, []), [[], []])

    def test_tools_actually_overlap_and_results_follow_input_order(self):
        barrier = threading.Barrier(2)
        finished = []

        class Blocking:
            def perform(self, **kwargs):
                barrier.wait(timeout=2)
                if kwargs["text"] == "first":
                    time.sleep(.03)
                finished.append(kwargs["text"])
                return kwargs["text"]

        calls = [{"id": str(i), "function": {
            "name": text, "arguments": json.dumps({"text": text})}}
            for i, text in enumerate(("first", "second"))]
        messages, _ = self.tool_results({"first": Blocking(), "second": Blocking()}, calls)
        self.assertEqual(finished, ["second", "first"])
        self.assertEqual([m["content"] for m in messages], ["first", "second"])

    def test_same_mutable_agent_calls_match_sequential_input_order(self):
        class Stateful:
            def __init__(self):
                self.count = 0

            def perform(self, user):
                self.user = user
                self.count += 1
                time.sleep(.025)
                return f"{self.count}:{self.user}"

        calls = [{"id": str(i), "function": {
            "name": "Stateful", "arguments": json.dumps({"user": user})}}
            for i, user in enumerate(("alice", "bob"))]
        messages, _ = self.tool_results({"Stateful": Stateful()}, calls)
        self.assertEqual([m["content"] for m in messages], ["1:alice", "2:bob"])


class HTTPTests(unittest.TestCase):
    def setUp(self):
        self.patch = mock.patch.object(host, "_request_agents", return_value={})
        self.patch.start()
        self.addCleanup(self.patch.stop)
        self.server = host.ThreadingHTTPServer(("127.0.0.1", 0), host.Handler)
        self.thread = threading.Thread(target=self.server.serve_forever)
        self.thread.start()
        self.addCleanup(self.close_server)
        self.url = f"http://127.0.0.1:{self.server.server_port}"

    def close_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    def post(self, path, body):
        raw = body if isinstance(body, bytes) else json.dumps(body).encode()
        request = urllib.request.Request(self.url + path, data=raw,
                                         headers={"Content-Type": "application/json"})
        try:
            response = urllib.request.urlopen(request, timeout=5)
        except urllib.error.HTTPError as error:
            response = error
        with response:
            return response.status, response.headers, response.read()

    @staticmethod
    def model(messages, tools, emit=None):
        return {"model": "test-model", "choices": [{"message": {
            "role": "assistant", "content": "response: " + messages[-1]["content"]}}]}

    def test_stream_final_equals_chat_and_is_not_genuinely_token_streamed(self):
        events = []
        with mock.patch.object(host, "model_call", side_effect=self.model):
            body = {"user_input": "hello", "session_id": "fixed"}
            code, _, raw = self.post("/chat", body)
            stream_code, headers, stream = self.post("/chat/stream", body)
        self.assertEqual((code, stream_code), (200, 200))
        self.assertEqual(headers["Content-Type"], "text/event-stream")
        self.assertEqual(headers["Cache-Control"], "no-cache")
        self.assertTrue(stream.endswith(b"\n\n"))
        events = [json.loads(line[6:]) for line in stream.decode().splitlines() if line.startswith("data: ")]
        self.assertEqual(events[0], {"type": "delta", "text": "response: hello"})
        final = events[-1]
        self.assertEqual(final.pop("type"), "done")
        self.assertIs(final.pop("streamed"), False)
        self.assertEqual(final, json.loads(raw))

    def test_invalid_stream_requests_keep_json_status_and_chat_errors(self):
        for body in ({}, [], {"user_input": 5}, b"not-json",
                     {"user_input": "", "conversation_history": "bad"}):
            with self.subTest(body=body):
                regular = self.post("/chat", body)
                streamed = self.post("/chat/stream", body)
                self.assertEqual(regular[0], 400)
                self.assertEqual(streamed[0], 400)
                self.assertEqual(regular[2], streamed[2])
                self.assertEqual(streamed[1]["Content-Type"], "application/json")
        host._request_agents.assert_not_called()

    def test_stream_emits_tool_rounds_and_final_fallback(self):
        agent = types.SimpleNamespace(perform=lambda **kw: "tool result", to_tool=lambda: {})
        call = {"id": "call", "function": {"name": "Echo", "arguments": "{}"}}
        responses = [
            {"model": "test", "choices": [{"message": {
                "role": "assistant", "content": None, "tool_calls": [call]}}]}
            for _ in range(3)]
        responses.append({"model": "test", "choices": [{"message": {
            "role": "assistant", "content": "fallback"}}]})
        with mock.patch.object(host, "_request_agents", return_value={"Echo": agent}), \
                mock.patch.object(host, "model_call", side_effect=responses):
            code, _, raw = self.post("/chat/stream", {"user_input": "loop", "session_id": "fixed"})
        events = [json.loads(line[6:]) for line in raw.decode().splitlines() if line.startswith("data: ")]
        self.assertEqual(code, 200)
        self.assertEqual([e["type"] for e in events], ["agent", "agent", "agent", "delta", "done"])
        self.assertEqual(events[-1]["response"], "fallback")
        self.assertEqual(events[-1]["agent_logs"], "\n".join(["[Echo] tool result"] * 3))

    def test_failure_after_stream_start_is_error_not_done(self):
        call = {"id": "call", "function": {"name": "Nobody", "arguments": "{}"}}
        response = {"model": "test", "choices": [{"message": {
            "role": "assistant", "content": None, "tool_calls": [call]}}]}
        with mock.patch.object(host, "model_call", side_effect=[response, RuntimeError("model broke")]):
            code, _, raw = self.post("/chat/stream", {"user_input": "loop", "session_id": "fixed"})
        events = [json.loads(line[6:]) for line in raw.decode().splitlines() if line.startswith("data: ")]
        self.assertEqual(code, 200)
        self.assertEqual(events[-1], {"type": "error", "error": "model broke"})
        self.assertNotIn("done", [e["type"] for e in events])

    def test_error_before_first_event_is_json_500(self):
        with mock.patch.object(host, "model_call", side_effect=RuntimeError("no token")):
            code, headers, raw = self.post("/chat/stream", {"user_input": "hello"})
        self.assertEqual(code, 500)
        self.assertEqual(headers["Content-Type"], "application/json")
        self.assertEqual(json.loads(raw), {"error": "no token"})

    @contextlib.contextmanager
    def upstream(self, respond):
        class Upstream(host.BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                respond(self, body)

        with host.ThreadingHTTPServer(("127.0.0.1", 0), Upstream) as server:
            thread = threading.Thread(target=server.serve_forever)
            thread.start()
            try:
                session = {"token": "test-token", "api": f"http://127.0.0.1:{server.server_port}"}
                with mock.patch.object(host, "copilot_session", return_value=session):
                    yield
            finally:
                server.shutdown()
                thread.join(timeout=5)

    def test_upstream_tokens_arrive_before_completion_without_duplicate_delta(self):
        release = threading.Event()
        errors = []
        requests = []

        def respond(handler, body):
            requests.append(body)
            handler.send_response(200)
            handler.send_header("Content-Type", "text/event-stream")
            handler.end_headers()
            first = {"model": "upstream", "choices": [{"index": 0, "delta": {"content": "one "}}]}
            handler.wfile.write(f"data: {json.dumps(first)}\n\n".encode())
            handler.wfile.flush()
            if not release.wait(timeout=3):
                errors.append("client did not receive first delta before upstream completion")
            time.sleep(.03)
            second = {"model": "upstream", "choices": [{
                "index": 0, "delta": {"content": "two"}, "finish_reason": "stop"}]}
            handler.wfile.write(f"data: {json.dumps(second)}\n\ndata: [DONE]\n\n".encode())
            handler.wfile.flush()

        with self.upstream(respond):
            request = urllib.request.Request(
                self.url + "/chat/stream", data=b'{"user_input":"count","session_id":"fixed"}')
            try:
                with urllib.request.urlopen(request, timeout=5) as response:
                    first = response.readline()
                    self.assertEqual(json.loads(first[6:]), {"type": "delta", "text": "one "})
                    release.set()
                    raw = first + response.read()
            finally:
                release.set()
        self.assertFalse(errors, errors)
        self.assertIs(requests[0]["stream"], True)
        events = [json.loads(line[6:]) for line in raw.decode().splitlines() if line.startswith("data: ")]
        self.assertEqual([e["type"] for e in events], ["delta", "delta", "done"])
        self.assertEqual(events[-1]["response"], "one two")
        self.assertEqual(events[-1]["model"], "upstream")
        self.assertIs(events[-1]["streamed"], True)

    def test_json_upstream_fallback_is_not_marked_streamed(self):
        def respond(handler, body):
            payload = json.dumps(self.model(body["messages"], body.get("tools"))).encode()
            handler.send_response(200)
            handler.send_header("Content-Type", "application/json")
            handler.send_header("Content-Length", str(len(payload)))
            handler.end_headers()
            handler.wfile.write(payload)

        with self.upstream(respond):
            code, _, raw = self.post("/chat/stream", {"user_input": "hello", "session_id": "fixed"})
        self.assertEqual(code, 200)
        events = [json.loads(line[6:]) for line in raw.decode().splitlines() if line.startswith("data: ")]
        self.assertEqual([e["type"] for e in events], ["delta", "done"])
        self.assertIs(events[-1]["streamed"], False)
        self.assertEqual(events[-1]["response"], "response: hello")


class ModelStreamingTests(unittest.TestCase):
    class Response(io.BytesIO):
        def __init__(self, content, content_type="text/event-stream"):
            super().__init__(content)
            self.headers = Message()
            self.headers["Content-Type"] = content_type

    @staticmethod
    def sse(chunks, done=True):
        raw = "".join(f"data: {json.dumps(chunk, ensure_ascii=False)}\r\n\r\n" for chunk in chunks)
        return (raw + ("data: [DONE]\r\n\r\n" if done else "")).encode()

    def setUp(self):
        patch = mock.patch.object(host, "copilot_session", return_value={
            "token": "test-token", "api": "http://model.test"})
        patch.start()
        self.addCleanup(patch.stop)

    def test_fragmented_tool_calls_choice_order_and_unicode(self):
        chunks = [
            {"model": "actual", "choices": [{"index": 0, "delta": {
                "role": "assistant", "tool_calls": [
                    {"index": 1, "id": "second", "type": "function",
                     "function": {"name": "E", "arguments": '{"text":'}},
                    {"index": 0, "id": "first", "type": "function",
                     "function": {"name": "E", "arguments": '{"text":'}}]}}]},
            {"choices": [{"index": 0, "delta": {"tool_calls": [
                {"index": 0, "function": {"name": "cho", "arguments": '"one"}'}},
                {"index": 1, "function": {"name": "cho", "arguments": '"two"}'}}]}}]},
            {"choices": [{"index": 1, "delta": {"content": "ignored"}},
                         {"index": 0, "delta": {"content": "caf\u00e9"}}]},
            {"choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]},
            {"choices": [], "usage": {"total_tokens": 10}},
        ]
        events = []
        with mock.patch.object(host.urllib.request, "urlopen",
                               return_value=self.Response(self.sse(chunks))) as request:
            result = host.model_call([], [{"type": "function"}], emit=events.append)
        self.assertIs(json.loads(request.call_args.args[0].data)["stream"], True)
        message = result["choices"][0]["message"]
        self.assertEqual(result["model"], "actual")
        self.assertEqual(message["content"], "caf\u00e9")
        self.assertEqual([call["id"] for call in message["tool_calls"]], ["first", "second"])
        self.assertEqual([call["function"] for call in message["tool_calls"]], [
            {"name": "Echo", "arguments": '{"text":"one"}'},
            {"name": "Echo", "arguments": '{"text":"two"}'}])
        self.assertEqual(events, [{"type": "delta", "text": "caf\u00e9"}])

    def test_refused_stream_retries_once_without_stream_and_chat_never_requests_it(self):
        complete = {"model": "test", "choices": [{"message": {
            "role": "assistant", "content": "fallback"}}]}
        refused = urllib.error.HTTPError(
            "http://model.test", 400, "stream unsupported", {}, io.BytesIO(b"unsupported"))
        with mock.patch.object(host.urllib.request, "urlopen", side_effect=[
                refused, self.Response(json.dumps(complete).encode(), "application/json")]) as opened, \
                contextlib.redirect_stdout(io.StringIO()):
            events = []
            self.assertEqual(host.model_call([], None, emit=events.append), complete)
        self.assertEqual(events, [])
        self.assertEqual(opened.call_count, 2)
        self.assertTrue(json.loads(opened.call_args_list[0].args[0].data)["stream"])
        self.assertNotIn("stream", json.loads(opened.call_args_list[1].args[0].data))
        with mock.patch.object(host.urllib.request, "urlopen",
                               return_value=self.Response(json.dumps(complete).encode(),
                                                          "application/json")) as opened:
            self.assertEqual(host.model_call([], None), complete)
        self.assertNotIn("stream", json.loads(opened.call_args.args[0].data))

    def test_auth_error_is_not_retried(self):
        error = urllib.error.HTTPError("http://model.test", 401, "no auth", {}, io.BytesIO(b"no auth"))
        with mock.patch.object(host.urllib.request, "urlopen", side_effect=error) as opened:
            with self.assertRaisesRegex(RuntimeError, "401"):
                host.model_call([], None, emit=lambda event: None)
        self.assertEqual(opened.call_count, 1)

    def test_truncated_or_malformed_stream_is_not_replayed_as_fallback(self):
        chunk = {"choices": [{"index": 0, "delta": {"content": "partial"}}]}
        for raw in (self.sse([chunk], done=False), self.sse([chunk]),
                    self.sse([]), b"data: not json\n\n",
                    b'data: {"error":{"message":"failed"}}\n\n'):
            with self.subTest(raw=raw):
                events = []
                with mock.patch.object(host.urllib.request, "urlopen",
                                       return_value=self.Response(raw)) as opened:
                    with self.assertRaises((RuntimeError, ValueError)):
                        host.model_call([], None, emit=events.append)
                self.assertEqual(opened.call_count, 1)

    def test_synthesized_reply_after_whitespace_stream_is_emitted_and_not_marked_streamed(self):
        call = {"index": 0, "id": "call", "function": {"name": "Echo", "arguments": "{}"}}
        tool_chunk = {"model": "actual", "choices": [{"index": 0,
            "delta": {"tool_calls": [call]}, "finish_reason": "tool_calls"}]}
        whitespace = {"model": "actual", "choices": [{"index": 0,
            "delta": {"content": "   "}, "finish_reason": "stop"}]}
        agent = types.SimpleNamespace(perform=lambda **kw: "result", to_tool=lambda: {})
        responses = [self.Response(self.sse([tool_chunk])) for _ in range(3)]
        responses.append(self.Response(self.sse([whitespace])))
        events = []
        with mock.patch.object(host.urllib.request, "urlopen", side_effect=responses):
            code, body = host.run("(handle-chat-stream *request*)", {"Echo": agent},
                                  {"user_input": "loop", "session_id": "fixed"}, emit=events.append)
        self.assertEqual(code, 200)
        self.assertTrue(body["response"].startswith("I couldn't finish"))
        self.assertEqual(events[-2], {"type": "delta", "text": body["response"]})
        self.assertEqual(events[-1]["response"], body["response"])
        self.assertIs(events[-1]["streamed"], False)

    def test_final_toolless_tokens_are_not_duplicated_and_keep_true_marker(self):
        call = {"index": 0, "id": "call", "function": {"name": "Echo", "arguments": "{}"}}
        tool = {"model": "actual", "choices": [{"index": 0,
            "delta": {"tool_calls": [call]}, "finish_reason": "tool_calls"}]}
        final = [
            {"model": "actual", "choices": [{"index": 0, "delta": {"content": " final "}}]},
            {"model": "actual", "choices": [{"index": 0,
                "delta": {"content": "answer "}, "finish_reason": "stop"}]}]
        agent = types.SimpleNamespace(perform=lambda **kw: "result", to_tool=lambda: {})
        responses = [self.Response(self.sse([tool])) for _ in range(3)]
        responses.append(self.Response(self.sse(final)))
        events = []
        with mock.patch.object(host.urllib.request, "urlopen", side_effect=responses):
            code, body = host.run("(handle-chat-stream *request*)", {"Echo": agent},
                                  {"user_input": "loop", "session_id": "fixed"}, emit=events.append)
        self.assertEqual(code, 200)
        self.assertEqual(body["response"], "final answer")
        self.assertEqual([e["type"] for e in events], ["agent", "agent", "agent", "delta", "delta", "done"])
        self.assertIs(events[-1]["streamed"], True)

    def test_multiline_sse_comments_and_finish_without_done(self):
        raw = (b': heartbeat\n\nevent: message\n'
               b'data: {"model":"test",\n'
               b'data: "choices":[{"index":0,"delta":{"content":"ok"},"finish_reason":"stop"}]}\n\n')
        events = []
        result = host._read_model_stream(self.Response(raw), events.append)
        self.assertEqual(result["choices"][0]["message"]["content"], "ok")
        self.assertEqual(events, [{"type": "delta", "text": "ok"}])

    def test_final_toolless_stream_failure_is_not_swallowed(self):
        env = host.make_env({}, emit=lambda event: None)
        with mock.patch.object(host, "model_call", side_effect=RuntimeError("stream broke")):
            with self.assertRaisesRegex(RuntimeError, "stream broke"):
                env["model-call-safe"](host.L.json_to_lisp([]), host.L.json_to_lisp([]))


class BenchmarkTests(unittest.TestCase):
    def test_all_25_scenarios_and_threshold_exactly(self):
        oracle = bench.load_oracle()
        self.assertEqual(len(oracle.SCENARIOS), 25)
        with contextlib.redirect_stdout(io.StringIO()):
            for name, lispy, grail, expected in (
                ("noise", .915, .887, 0), ("relative only", 1.5, 1, 0),
                ("absolute boundary", 3, 1, 0), ("relative boundary", 12.5, 10, 0),
                ("absolute only", 23, 20, 0), ("both exceeded", 3.000001, 1, 1),
                ("both large", 12.500001, 10, 1)):
                with self.subTest(name=name):
                    self.assertEqual(bench.print_results([(name, lispy, grail)], []), expected)

    def test_comparison_checks_contract_and_interleaves_measurements(self):
        scenario = types.SimpleNamespace(name="sample")
        oracle = types.SimpleNamespace(
            SCENARIOS=[scenario], call_http=lambda *args: (200, {"response": "ok"}),
            contract_view=lambda scenario, code, body, **kwargs: (code, body))
        order = []

        def timed(oracle, url, scenario):
            order.append(url)
            return (1 if url == "lispy" else 2), (200, {"response": "ok"})

        with mock.patch.object(bench, "timed_call", side_effect=timed):
            self.assertEqual(bench.compare_scenarios(oracle, "lispy", "grail", samples=2),
                             [("sample", 1, 2)])
        self.assertEqual(order, ["lispy", "grail", "grail", "lispy"])
        with mock.patch.object(bench, "timed_call", side_effect=[
                (1, (200, {"response": "different"})), (2, (200, {"response": "ok"}))]):
            with self.assertRaisesRegex(RuntimeError, "contract mismatch"):
                bench.compare_scenarios(oracle, "lispy", "grail", samples=1)

    def test_failed_startup_stops_owned_process(self):
        process = mock.Mock()
        oracle = types.SimpleNamespace(stop_process=mock.Mock())
        with tempfile.TemporaryDirectory() as temporary, \
                mock.patch.object(bench.subprocess, "Popen", return_value=process), \
                mock.patch.object(bench, "wait_ready", side_effect=RuntimeError("not ready")):
            with self.assertRaisesRegex(RuntimeError, "not ready"):
                with contextlib.ExitStack() as stack:
                    bench.launch(stack, oracle, ["python"], {}, Path(temporary),
                                 Path(temporary) / "server.log", "http://localhost/health")
        oracle.stop_process.assert_called_once_with(process)


if __name__ == "__main__":
    unittest.main(verbosity=2)
