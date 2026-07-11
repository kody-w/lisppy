import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import lisp
from tests.support import run_cli


class ExecutionLimitTests(unittest.TestCase):
    def test_five_step_expression_boundary(self):
        source = "(+ 1 2)"
        passing = lisp.make_global_env(
            limits=lisp.ExecutionLimits(max_steps=5)
        )
        failing = lisp.make_global_env(
            limits=lisp.ExecutionLimits(max_steps=4)
        )
        self.assertEqual(lisp.run_string(source, passing), 3)
        with self.assertRaises(lisp.ExecutionLimitExceeded) as raised:
            lisp.run_string(source, failing)
        self.assertEqual(raised.exception.resource, "steps")

    def test_recursive_call_depth_is_bounded(self):
        env = lisp.make_global_env(
            limits=lisp.ExecutionLimits(
                max_steps=10_000,
                max_call_depth=8,
            )
        )
        source = """
        (define (loop n) (loop (+ n 1)))
        (loop 0)
        """
        with self.assertRaises(lisp.ExecutionLimitExceeded) as raised:
            lisp.run_string(source, env)
        self.assertEqual(raised.exception.resource, "call_depth")

    def test_range_is_bounded_before_allocation(self):
        env = lisp.make_global_env(
            limits=lisp.ExecutionLimits(max_collection_items=3)
        )
        with self.assertRaises(lisp.ExecutionLimitExceeded) as raised:
            lisp.run_string("(range 4)", env)
        self.assertEqual(raised.exception.resource, "collection_items")

    def test_list_zip_and_flatten_are_bounded(self):
        for source in (
            "(list 1 2 3 4)",
            "(zip (list 1 2 3 4) (list 5 6 7 8))",
            "(flatten (list (list 1 2) (list 3 4)))",
            "(reverse '(1 2 3 4))",
        ):
            with self.subTest(source=source):
                env = lisp.make_global_env(
                    limits=lisp.ExecutionLimits(max_collection_items=3)
                )
                with self.assertRaises(lisp.ExecutionLimitExceeded):
                    lisp.run_string(source, env)

    def test_reader_depth_is_bounded_without_recursion_error(self):
        env = lisp.make_global_env(
            limits=lisp.ExecutionLimits(max_reader_depth=20)
        )
        source = "(" * 30 + "1" + ")" * 30
        with self.assertRaises(lisp.ExecutionLimitExceeded) as raised:
            lisp.run_string(source, env)
        self.assertEqual(raised.exception.resource, "reader_depth")

    def test_source_bytes_are_bounded(self):
        env = lisp.make_global_env(
            limits=lisp.ExecutionLimits(max_source_bytes=4)
        )
        with self.assertRaises(lisp.ExecutionLimitExceeded) as raised:
            lisp.run_string("(+ 1 2)", env)
        self.assertEqual(raised.exception.resource, "source_bytes")

    def test_cli_limit_failure_is_clean(self):
        result = run_cli(
            "--max-steps",
            "4",
            "-e",
            "(+ 1 2)",
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("execution limit exceeded: steps", result.stderr)
        self.assertNotIn("Traceback", result.stderr)

    def test_cli_output_limit_is_clean(self):
        result = run_cli(
            "--max-output-bytes",
            "3",
            "-e",
            '(display "four")',
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("execution limit exceeded: output_bytes", result.stderr)
        self.assertEqual(result.stdout, "")

    def test_custom_output_sink_is_still_bounded(self):
        captured = []
        env = lisp.make_global_env(
            limits=lisp.ExecutionLimits(max_output_bytes=3),
            output=captured.append,
        )
        with self.assertRaises(lisp.ExecutionLimitExceeded):
            lisp.run_string('(display "four")', env)
        self.assertEqual(captured, [])

    def test_result_collection_limits_cover_strings_maps_and_json(self):
        for source in (
            '"four"',
            "'(1 2 3 4)",
            '(make-dict "a" 1 "b" 2 "c" 3 "d" 4)',
            '(make-dict "four" 1)',
            '(json-parse "[1,2,3,4]")',
            '(string-append "ab" "cd")',
        ):
            with self.subTest(source=source):
                env = lisp.make_global_env(
                    limits=lisp.ExecutionLimits(max_collection_items=3)
                )
                with self.assertRaises(lisp.ExecutionLimitExceeded):
                    lisp.run_string(source, env)


if __name__ == "__main__":
    unittest.main()
