"""Compatibility and lazy-loading oracle for the hosting extraction."""
import json
import unittest
from pathlib import Path
from unittest import mock

import lisp
from tests.support import run_cli, run_python


class HostedAliasTests(unittest.TestCase):
    def test_language_import_and_execution_do_not_load_hosting(self):
        result = run_python(
            "-c",
            "import sys, lisp; "
            "assert 'lispy_hosted' not in sys.modules; "
            "assert lisp.run_file('examples/hello.lispy') is lisp.NIL; "
            "assert lisp.run_string('(get (json-parse \"{\\\\\\\"x\\\\\\\":7}\") \"x\")') == 7; "
            "assert lisp.LispyVM(profile='core').execute('(+ 2 3)').value == 5; "
            "assert 'lispy_hosted' not in sys.modules",
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_old_names_resolve_to_the_hosted_implementation(self):
        import lispy_hosted

        for name in lisp._HOSTED_NAMES:
            with self.subTest(name=name):
                self.assertIs(getattr(lisp, name), getattr(lispy_hosted, name))
                self.assertIn(name, dir(lisp))
        with self.assertRaises(AttributeError):
            getattr(lisp, "not_a_lispy_export")

    def test_from_import_and_package_facade_keep_identity(self):
        result = run_python(
            "-c",
            "from lisp import run_hosted_governor, registered_source; "
            "import lisp, lisppy, lispy_hosted; "
            "assert run_hosted_governor is lispy_hosted.run_hosted_governor; "
            "assert lisppy.run_hosted_governor is run_hosted_governor; "
            "assert registered_source is lispy_hosted.registered_source; "
            "assert lispy_hosted.NIL is lisp.NIL; "
            "assert lispy_hosted.LispError is lisp.LispError",
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_hosting_can_be_imported_before_language(self):
        result = run_python(
            "-c",
            "import lispy_hosted, lisp; "
            "assert lisp.run_hosted_governor is lispy_hosted.run_hosted_governor; "
            "assert lisp.run_hosted_governor('nil', inputs={}, mutable_outputs={})"
            "['status'] == 'accepted'",
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_wildcard_import_preserves_public_host_names(self):
        result = run_python(
            "-c",
            "from lisp import *; import lispy_hosted; "
            "assert run_hosted_governor is lispy_hosted.run_hosted_governor; "
            "assert registered_source is lispy_hosted.registered_source; "
            "assert HostedValidationError is lispy_hosted.HostedValidationError; "
            "assert subprocess is lispy_hosted.subprocess",
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_script_entrypoint_catches_shared_host_errors(self):
        result = run_cli("--replay", "README.md", "--json")
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stderr, "")
        self.assertEqual(json.loads(result.stdout)["error"]["code"], "invalid_data")

    def test_worker_manifest_covers_both_implementation_files(self):
        self.assertIn("lispy_hosted.py", lisp.INSTALLED_REQUIRED_PATHS)
        files = {item["path"] for item in lisp._source_inventory()}
        self.assertIn("lisp.py", files)
        self.assertIn("lispy_hosted.py", files)

    def test_forwarded_worker_environment_keeps_state_isolated(self):
        environment = lisp._worker_environment("/isolated-worker")
        self.assertEqual(environment["STATE_DIR"], "/isolated-worker")
        self.assertNotIn("_language.STATE_DIR", environment)

    def test_forwarded_artifact_digest_binds_language_and_hosting(self):
        original_read = Path.read_bytes
        original_digest = lisp._artifact_sha256()
        for filename in ("lisp.py", "lispy_hosted.py"):
            def read(path):
                content = original_read(path)
                return content + b"\n# modified\n" if path.name == filename else content

            with self.subTest(filename=filename), mock.patch.object(Path, "read_bytes", read):
                self.assertNotEqual(lisp._artifact_sha256(), original_digest)


if __name__ == "__main__":
    unittest.main()
