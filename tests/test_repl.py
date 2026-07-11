import contextlib
import io
import unittest
from unittest import mock

import lisp


class ReplTests(unittest.TestCase):
    def test_evaluation_interrupt_resets_and_session_continues(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            env = lisp.make_global_env()
            with mock.patch(
                "builtins.input",
                side_effect=["(+ 1 2)", "(+ 2 3)", EOFError()],
            ), mock.patch.object(
                lisp,
                "evaluate",
                side_effect=[KeyboardInterrupt(), 5],
            ):
                lisp.repl(env)
        text = output.getvalue()
        self.assertIn("; interrupted", text)
        self.assertIn("=> 5", text)
        self.assertIn("; farewell", text)
        self.assertNotIn("Traceback", text)

    def test_repl_result_uses_bounded_writer(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            env = lisp.make_global_env(
                limits=lisp.ExecutionLimits(max_output_bytes=3)
            )
            with mock.patch(
                "builtins.input",
                side_effect=['"four"', EOFError()],
            ):
                lisp.repl(env)
        self.assertIn("output_bytes", output.getvalue())


if __name__ == "__main__":
    unittest.main()
