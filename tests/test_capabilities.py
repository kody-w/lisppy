import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import lisp


class CapabilityTests(unittest.TestCase):
    def test_safe_profile_denies_python_execution(self):
        env = lisp.make_global_env()
        with self.assertRaises(lisp.CapabilityDenied):
            lisp.run_string('(rb-run "print(42)")', env)

    def test_trusted_profile_enables_python_execution(self):
        with mock.patch.object(lisp, "rb_run", return_value="trusted") as rb_run:
            env = lisp.make_global_env(trusted=True)
            result = lisp.run_string('(rb-run "print(42)")', env)
        self.assertEqual(result, "trusted")
        rb_run.assert_called_once_with("print(42)")

    def test_runtime_reports_capabilities(self):
        safe = lisp.run_string("(runtime-info)", lisp.make_global_env())
        trusted = lisp.run_string(
            "(runtime-info)", lisp.make_global_env(trusted=True)
        )
        builtins = lisp.run_string("(builtin-manifest)", lisp.make_global_env())
        self.assertFalse(safe["trusted"])
        self.assertTrue(safe["stdlib_loaded"])
        self.assertEqual(
            safe["profiles"],
            [
                "lispy-core@1",
                "rappterbook.read",
                "rappterbook.plan",
            ],
        )
        self.assertIn("identity", builtins)
        self.assertNotIn("process.python", safe["capabilities"])
        self.assertTrue(trusted["trusted"])
        self.assertIn("process.python", trusted["capabilities"])

    def test_state_reads_cannot_escape_root(self):
        original = lisp.STATE_DIR
        try:
            with tempfile.TemporaryDirectory() as state_dir:
                lisp.set_state_dir(state_dir)
                with self.assertRaises(lisp.CapabilityDenied):
                    lisp.rb_state("../outside.json")
        finally:
            lisp.set_state_dir(original)

    def test_malformed_state_has_lispy_error(self):
        with tempfile.TemporaryDirectory() as state_dir:
            Path(state_dir, "agents.json").write_text(
                "[]",
                encoding="utf-8",
            )
            with self.assertRaises(lisp.LispError) as raised:
                lisp.rb_agent("agent", state_dir=state_dir)
        self.assertIn("agents.json must contain", str(raised.exception))

    def test_state_json_rejects_duplicates_and_non_finite_numbers(self):
        for content in ('{"x": 1, "x": 2}', '{"x": NaN}'):
            with self.subTest(content=content):
                with tempfile.TemporaryDirectory() as state_dir:
                    Path(state_dir, "stats.json").write_text(
                        content,
                        encoding="utf-8",
                    )
                    with self.assertRaises(lisp.InvalidDataError):
                        lisp.rb_state("stats.json", state_dir=state_dir)

    def test_soul_ids_and_bytes_are_bounded_utf8(self):
        with tempfile.TemporaryDirectory() as state_dir:
            memory = Path(state_dir, "memory")
            memory.mkdir()
            Path(memory, "agent.md").write_text("memory", encoding="utf-8")
            self.assertEqual(
                lisp.rb_soul("agent", state_dir=state_dir),
                "memory",
            )
            for agent_id in ("../agent", "memory/agent", "", "."):
                with self.subTest(agent_id=agent_id):
                    with self.assertRaises(lisp.InvalidDataError):
                        lisp.rb_soul(agent_id, state_dir=state_dir)
            Path(memory, "invalid.md").write_bytes(b"\xff")
            with self.assertRaises(lisp.InvalidDataError):
                lisp.rb_soul("invalid", state_dir=state_dir)
            Path(memory, "large.md").write_bytes(b"12345")
            with mock.patch.object(lisp, "MAX_STATE_BYTES", 4):
                with self.assertRaises(lisp.ExecutionLimitExceeded):
                    lisp.rb_soul("large", state_dir=state_dir)


if __name__ == "__main__":
    unittest.main()
