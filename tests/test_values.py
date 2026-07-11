import math
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import lisp


class Core1ValueTests(unittest.TestCase):
    def test_numeric_operations_reject_python_coercions_and_complex_values(self):
        for source in (
            "(+ #t 1)",
            '(* "x" 3)',
            "(zero? #f)",
            "(expt -1 0.5)",
        ):
            with self.subTest(source=source):
                with self.assertRaises(lisp.LispError):
                    lisp.run_string(source)

    def test_deep_pair_equality_is_iterative(self):
        left = lisp.NIL
        right = lisp.NIL
        for value in range(1500):
            left = lisp.Pair(value, left)
            right = lisp.Pair(value, right)
        env = lisp.make_global_env()
        env["left"] = left
        env["right"] = right
        self.assertTrue(lisp.run_string("(equal? left right)", env))

    def test_core_maps_reject_ambiguous_or_incomplete_keys(self):
        for source in (
            '(make-dict #t "bool" 1 "int")',
            '(make-dict \'x 1 "x" 2)',
            '(make-dict "a" 1 "b")',
            '(make-dict "a" 1 "a" 2)',
            '(dict-set (make-dict) 1 "value")',
        ):
            with self.subTest(source=source):
                with self.assertRaises(lisp.LispError):
                    lisp.run_string(source)

    def test_list_get_requires_an_exact_integer_index(self):
        for key in ("#t", "1.9"):
            with self.subTest(key=key):
                with self.assertRaises(lisp.LispError):
                    lisp.run_string(f"(get (list 10 20 30) {key})")

    def test_string_number_parser_is_ascii_finite_and_supports_exponents(self):
        self.assertEqual(lisp.run_string('(string->number "1e2")'), 100.0)
        self.assertEqual(lisp.run_string('(string->number "-0.0")'), -0.0)
        for value in ("1_0", "１２", "1e999", "NaN"):
            with self.subTest(value=value):
                with self.assertRaises(lisp.LispError):
                    lisp.run_string(f'(string->number "{value}")')

    def test_nested_json_null_is_lispy_nil_and_false(self):
        source = """
        (if (get (get (json-parse "{\\"x\\":[null]}") "x") 0)
            1
            2)
        """
        self.assertEqual(lisp.run_string(source), 2)

    def test_hosted_null_input_is_lispy_nil(self):
        receipt = lisp.run_hosted_governor(
            "(if optional (set! output 1) (set! output 2))",
            inputs={"optional": None},
            mutable_outputs={"output": 0},
        )
        self.assertEqual(receipt["status"], "accepted")
        self.assertEqual(receipt["outputs"]["output"], 2)

    def test_equality_is_type_aware_and_structural(self):
        self.assertFalse(lisp.run_string("(equal? #t 1)"))
        self.assertFalse(lisp.run_string("(= #t 1)"))
        self.assertTrue(lisp.run_string("(!= #t 1)"))
        self.assertFalse(lisp.run_string('(equal? \'x "x")'))
        self.assertTrue(
            lisp.run_string(
                "(equal? (cons 1 (cons 2 nil)) (cons 1 (cons 2 nil)))"
            )
        )
        self.assertTrue(lisp.run_string("(equal? 1 1.0)"))

    def test_pair_predicate_requires_nonempty_sequence(self):
        self.assertFalse(lisp.run_string("(pair? (list))"))
        self.assertTrue(lisp.run_string("(pair? (list 1))"))
        self.assertTrue(lisp.run_string("(pair? (cons 1 nil))"))

    def test_json_dump_rejects_nonfinite_symbols_and_improper_pairs(self):
        env = lisp.make_global_env()
        env["infinity"] = math.inf
        for source in (
            "(json-dump infinity)",
            "(json-dump 'symbol)",
            "(json-dump (cons 1 2))",
        ):
            with self.subTest(source=source):
                with self.assertRaises(lisp.InvalidDataError):
                    lisp.run_string(source, env)

    def test_wire_rejects_duplicate_canonical_map_keys(self):
        first = lisp.Pair(1, lisp.NIL)
        second = lisp.Pair(1, lisp.NIL)
        with self.assertRaises(lisp.WireEncodingError):
            lisp.to_wire({first: "a", second: "b"})


if __name__ == "__main__":
    unittest.main()
