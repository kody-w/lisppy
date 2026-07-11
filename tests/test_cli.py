import subprocess
import tempfile
import unittest
from pathlib import Path


from tests.support import CLI, ROOT, run_cli
import lisp


class CliTests(unittest.TestCase):
    def test_eval_prints_result(self):
        result = run_cli("-e", "(+ 1 2 3)")
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "6\n")
        self.assertEqual(result.stderr, "")

    def test_pipe_prints_result(self):
        result = run_cli(stdin="(+ 1 2 3)\n")
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "6\n")

    def test_runtime_error_is_nonzero(self):
        result = run_cli("-e", "missing-name")
        self.assertEqual(result.returncode, 1)
        self.assertIn("unbound variable: missing-name", result.stderr)
        self.assertNotIn("Traceback", result.stderr)

    def test_invalid_json_is_nonzero_without_traceback(self):
        result = run_cli("-e", '(json-parse "{")')
        self.assertEqual(result.returncode, 1)
        self.assertIn("invalid JSON", result.stderr)
        self.assertNotIn("Traceback", result.stderr)

    def test_invalid_programs_never_leak_python_tracebacks(self):
        for source in (
            "(% 1 0)",
            "(let 1 2)",
            "(if #t 1 2 3)",
            "(string-split 1)",
            "(string-upcase 1)",
        ):
            with self.subTest(source=source):
                result = run_cli("-e", source)
                self.assertEqual(result.returncode, 1)
                self.assertIn("; error:", result.stderr)
                self.assertNotIn("Traceback", result.stderr)

    def test_invalid_stdin_bytes_fail_without_traceback(self):
        result = subprocess.run(
            CLI,
            input=b"\xff",
            capture_output=True,
            cwd=ROOT,
            timeout=20,
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn(b"stdin is not valid text", result.stderr)
        self.assertNotIn(b"Traceback", result.stderr)

    def test_invalid_script_bytes_fail_without_traceback(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "invalid.lisp"
            path.write_bytes(b"\xff")
            result = run_cli(str(path))
            self.assertEqual(result.returncode, 1)
            self.assertIn("script is not valid UTF-8", result.stderr)
            self.assertNotIn("Traceback", result.stderr)
            with self.assertRaises(lisp.LispSyntaxError):
                lisp.run_file(str(path))

    def test_script_size_is_bounded_before_decode(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "large.lisp"
            path.write_bytes(b"12345")
            result = run_cli(
                "--max-source-bytes",
                "4",
                str(path),
            )
        self.assertEqual(result.returncode, 1)
        self.assertIn("source_bytes", result.stderr)
        self.assertNotIn("Traceback", result.stderr)

    def test_special_modes_reject_inapplicable_flags(self):
        cases = [
            (
                [
                    "--doctor",
                    "inventory@1",
                    "--doctor-mode",
                    "source",
                    "--expect-bundle",
                    "0" * 64,
                ],
                "--expect-bundle",
            ),
            (
                [
                    "--replay",
                    "missing.json",
                    "--expect-inventory",
                    "0" * 64,
                ],
                "--expect-inventory",
            ),
            (
                [
                    "--doctor",
                    "inventory@1",
                    "--doctor-mode",
                    "source",
                    "--max-steps",
                    "100000",
                ],
                "--max-steps",
            ),
            (["--jsonl", "--trusted"], "--trusted"),
            (["--jsonl", "--json"], "--json"),
            (["--replay", "", "-e", "(+ 1 2)"], "--replay"),
            (["--export-replay", "", "-e", "(+ 1 2)"], "--export-replay"),
        ]
        for arguments, option in cases:
            with self.subTest(arguments=arguments):
                result = run_cli(*arguments)
                self.assertEqual(result.returncode, 2)
                output = result.stdout if "--json" in arguments else result.stderr
                self.assertIn(option, output)

    def test_json_usage_errors_are_one_structured_line(self):
        result = run_cli(
            "--json",
            "--replay",
            "",
            "-e",
            "(+ 1 2)",
        )
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stderr, "")
        response = __import__("json").loads(result.stdout)
        self.assertEqual(response["api"], "lispy.error/v1")
        self.assertEqual(response["error"]["code"], "invalid_arguments")
        self.assertNotIn("usage:", result.stdout)

    def test_abbreviations_empty_state_and_conflicting_limits_fail(self):
        for arguments in (
            ["--state", "examples/sample-state", "-e", "1"],
            ["--state-dir", "", "-e", "1"],
            ["--unlimited", "--max-steps", "1", "-e", "1"],
        ):
            with self.subTest(arguments=arguments):
                result = run_cli(*arguments)
                self.assertEqual(result.returncode, 2)
                self.assertNotIn("Traceback", result.stderr)

    def test_json_operational_errors_have_stable_codes(self):
        result = run_cli(
            "--json",
            "--replay",
            "missing-replay.json",
        )
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stderr, "")
        response = __import__("json").loads(result.stdout)
        self.assertEqual(response["error"]["code"], "host_io_error")

    def test_help_and_version(self):
        help_result = run_cli("--help")
        version_result = run_cli("--version")
        self.assertEqual(help_result.returncode, 0)
        self.assertIn("--trusted", help_result.stdout)
        self.assertEqual(version_result.stdout, "LisPy 0.24.0 (lispy-core@1)\n")

    def test_hello_example(self):
        result = run_cli("examples/hello.lisp")
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "Hello from LisPy: 6\n")


if __name__ == "__main__":
    unittest.main()
