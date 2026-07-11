import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import lisp


class LispyVMTests(unittest.TestCase):
    def test_execute_captures_output_and_value(self):
        vm = lisp.LispyVM()
        result = vm.execute('(begin (display "hello") (+ 2 3))')
        self.assertTrue(result.ok)
        self.assertEqual(result.value, 5)
        self.assertEqual(result.output, "hello")
        self.assertIsNone(result.error)
        json.dumps(result.as_dict(), allow_nan=False)

    def test_execute_returns_structured_error_with_partial_output(self):
        vm = lisp.LispyVM()
        result = vm.execute('(begin (display "before") (error "boom"))')
        self.assertFalse(result.ok)
        self.assertEqual(result.output, "before")
        self.assertEqual(result.error["phase"], "evaluate")
        self.assertIn("boom", result.error["message"])

    def test_vms_keep_state_roots_isolated(self):
        original = lisp.STATE_DIR
        with tempfile.TemporaryDirectory() as first, tempfile.TemporaryDirectory() as second:
            Path(first, "stats.json").write_text(
                '{"value": 1}',
                encoding="utf-8",
            )
            Path(second, "stats.json").write_text(
                '{"value": 2}',
                encoding="utf-8",
            )
            first_vm = lisp.LispyVM(state_root=first)
            second_vm = lisp.LispyVM(state_root=second)
            lisp.set_state_dir(second)
            try:
                self.assertEqual(
                    first_vm.execute('(get (rb-state "stats.json") "value")').value,
                    1,
                )
                self.assertEqual(
                    second_vm.execute('(get (rb-state "stats.json") "value")').value,
                    2,
                )
            finally:
                lisp.set_state_dir(original)

    def test_output_limit_is_structured(self):
        vm = lisp.LispyVM(
            limits=lisp.ExecutionLimits(max_output_bytes=3)
        )
        result = vm.execute('(display "four")')
        self.assertFalse(result.ok)
        self.assertEqual(result.error["resource"], "output_bytes")
        self.assertEqual(result.output, "")

    def test_unlimited_vm_accepts_portable_values(self):
        vm = lisp.LispyVM(limits=lisp.ExecutionLimits.unlimited())
        result = vm.execute("'(1 2 3)")
        self.assertTrue(result.ok)
        self.assertEqual(result.value, [1, 2, 3])

    def test_result_envelopes_are_wire_safe_for_all_portable_values(self):
        for source, tag in (
            ("'symbol", "symbol"),
            ("(cons 1 2)", "pair"),
            ("nil", "nil"),
        ):
            with self.subTest(source=source):
                result = lisp.LispyVM().execute(source)
                self.assertTrue(result.ok)
                wire = result.as_wire_dict()
                self.assertEqual(wire["api"], "lispy.execution-result/v1")
                self.assertEqual(wire["value"]["tag"], tag)
                json.dumps(result.as_dict(), allow_nan=False)
                json.dumps(wire, allow_nan=False)

    def test_nonportable_result_becomes_a_serialization_error(self):
        result = lisp.LispyVM().execute("(lambda (x) x)")
        self.assertFalse(result.ok)
        self.assertEqual(result.error["category"], "serialization")


if __name__ == "__main__":
    unittest.main()
