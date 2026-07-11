import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import lisp


class Core1SequenceTests(unittest.TestCase):
    def test_nil_list_and_pair_share_consumer_protocol(self):
        cases = {
            "nil": [],
            "(list 1 2)": [1, 2],
            "(cons 1 (cons 2 nil))": [1, 2],
        }
        transforms = {
            "reverse": lambda values: list(reversed(values)),
            "sort": lambda values: sorted(values),
            "take": lambda values: values[:1],
            "drop": lambda values: values[1:],
            "map identity": lambda values: values,
            "filter (lambda (x) #t)": lambda values: values,
        }
        for source, values in cases.items():
            for operation, expected in transforms.items():
                with self.subTest(source=source, operation=operation):
                    suffix = " 1" if operation in ("take", "drop") else ""
                    result = lisp.run_string(
                        f"({operation} {source}{suffix})"
                    )
                    self.assertEqual(result, expected(values))

    def test_reduce_apply_nth_last_and_append(self):
        self.assertEqual(lisp.run_string("(reduce + nil 7)"), 7)
        self.assertEqual(lisp.run_string("(apply list nil)"), [])
        self.assertEqual(
            lisp.run_string(
                "(apply + (cons 1 (cons 2 (cons 3 nil))))"
            ),
            6,
        )
        self.assertIs(lisp.run_string("(nth nil 0)"), lisp.NIL)
        self.assertEqual(
            lisp.run_string("(last (cons 1 (cons 2 nil)))"),
            2,
        )
        self.assertEqual(
            lisp.run_string(
                "(append (cons 1 (cons 2 nil)) (list 3 4))"
            ),
            [1, 2, 3, 4],
        )

    def test_multi_map_and_zip_normalize_every_input(self):
        self.assertEqual(
            lisp.run_string(
                "(map + (cons 1 (cons 2 nil)) (list 10))"
            ),
            [11],
        )
        self.assertEqual(lisp.run_string("(map + (list 1 2) nil)"), [])
        self.assertEqual(
            lisp.run_string(
                "(zip (cons 1 (cons 2 nil)) (list 3 4))"
            ),
            [[1, 3], [2, 4]],
        )

    def test_improper_and_cyclic_pairs_fail_before_iteration(self):
        for operation in (
            "append",
            "reverse",
            "sort",
            "map identity",
            "filter (lambda (x) #t)",
            "reduce +",
            "for-each identity",
            "apply list",
            "zip",
        ):
            with self.subTest(operation=operation):
                with self.assertRaisesRegex(lisp.LispError, "proper list"):
                    lisp.run_string(f"({operation} (cons 1 2))")

        cyclic = lisp.Pair(1, lisp.NIL)
        cyclic.cdr = cyclic
        with self.assertRaisesRegex(lisp.LispError, "cyclic pair"):
            lisp._proper_list_view(cyclic)

    def test_pair_and_zip_limits_cannot_be_bypassed(self):
        env = lisp.make_global_env(
            limits=lisp.ExecutionLimits(max_collection_items=3)
        )
        with self.assertRaises(lisp.ExecutionLimitExceeded):
            lisp.run_string("(zip '(1 2 3 4))", env)

    def test_string_join_uses_the_logical_list_contract(self):
        self.assertEqual(
            lisp.run_string(
                '(string-join (cons "a" (cons "b" nil)) ",")'
            ),
            "a,b",
        )
        with self.assertRaisesRegex(lisp.LispError, "proper list"):
            lisp.run_string('(string-join (cons "a" "b") ",")')


if __name__ == "__main__":
    unittest.main()
