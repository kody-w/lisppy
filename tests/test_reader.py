import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import lisp


class ReaderTests(unittest.TestCase):
    def test_tri_state_completeness(self):
        complete = ("", "; (", "(if)", "(+ 1 ; )\n2)")
        incomplete = ("(", "[", "'", '"abc')
        invalid = ("(]", ")")
        for source in complete:
            with self.subTest(source=source):
                self.assertEqual(
                    lisp.read_source(source).state,
                    lisp.ReadState.COMPLETE,
                )
        for source in incomplete:
            with self.subTest(source=source):
                self.assertEqual(
                    lisp.read_source(source).state,
                    lisp.ReadState.INCOMPLETE,
                )
        for source in invalid:
            with self.subTest(source=source):
                self.assertEqual(
                    lisp.read_source(source).state,
                    lisp.ReadState.INVALID,
                )
        self.assertEqual(
            lisp.read_source("1e999").state,
            lisp.ReadState.INVALID,
        )

    def test_mismatched_delimiter_reports_source_position(self):
        result = lisp.read_source("(\n]", source_name="case.lisp")
        self.assertEqual(result.state, lisp.ReadState.INVALID)
        self.assertEqual(result.error.source_name, "case.lisp")
        self.assertEqual(result.error.line, 2)
        self.assertEqual(result.error.column, 1)
        self.assertIn("expected ')' for '(' opened at 1:1", str(result.error))

    def test_tokens_carry_positions_without_leaking_into_ast(self):
        tokens = lisp.tokenize('(a\n ["x"])', source_name="case.lisp")
        positions = [(str(token), token.line, token.column) for token in tokens]
        self.assertEqual(
            positions,
            [
                ("(", 1, 1),
                ("a", 1, 2),
                ("[", 2, 2),
                ('"x"', 2, 3),
                ("]", 2, 6),
                (")", 2, 7),
            ],
        )
        ast = lisp.parse("'[a (1)]")
        self.assertEqual(
            ast,
            [[lisp.Symbol("quote"), [lisp.Symbol("a"), [1]]]],
        )
        self.assertTrue(all(type(node) is list for node in (ast, ast[0], ast[0][1])))

    def test_special_forms_preflight_before_side_effects(self):
        env = lisp.make_global_env()
        lisp.run_string("(define x 0)", env)
        with self.assertRaises(lisp.LispError):
            lisp.run_string(
                "(let ((a (set! x 1)) malformed) a)",
                env,
            )
        self.assertEqual(env["x"], 0)
        with self.assertRaisesRegex(lisp.LispError, "else clause must be last"):
            lisp.run_string("(cond (else 1) (#t 2))", env)
        output = lisp.BoundedOutput(100)
        guarded = lisp.make_global_env(output=output)
        with self.assertRaisesRegex(lisp.LispError, "unbound variable"):
            lisp.run_string('(set! missing (display "leak"))', guarded)
        self.assertEqual(output.getvalue(), "")
        guarded.protected_definitions.add("protected")
        with self.assertRaises(lisp.CapabilityDenied):
            lisp.run_string(
                '(define protected (display "leak"))',
                guarded,
            )
        self.assertEqual(output.getvalue(), "")

    def test_existing_datum_macros_remain_usable(self):
        source = """
        (begin
          (define x 0)
          (define-macro (ignore form) 7)
          (ignore (set! x 1))
          x)
        """
        self.assertEqual(lisp.run_string(source), 0)
        unless = """
        (begin
          (define-macro (unless test body)
            (list 'if test nil body))
          (unless #f 7))
        """
        self.assertEqual(lisp.run_string(unless), 7)

    def test_quasiquote_has_explicit_unsupported_error(self):
        result = lisp.LispyVM().execute("`x")
        self.assertFalse(result.ok)
        self.assertEqual(result.error["category"], "unsupported")


if __name__ == "__main__":
    unittest.main()
