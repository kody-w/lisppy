import base64
import contextlib
import hashlib
import io
import json
import sys
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import lisp
import lisppy
from lisppy.effects import InMemoryIdempotencyStore
from tests.support import run_module, run_python


class PackageFacadeTests(unittest.TestCase):
    def test_public_facade_and_version(self):
        self.assertEqual(lisppy.__version__, lisp.VERSION)
        self.assertIs(lisppy.LispyVM, lisp.LispyVM)
        self.assertIn("run_hosted_governor", lisppy.__all__)
        self.assertIn("run_demo", lisppy.__all__)
        self.assertIn("run_demo", dir(lisppy))
        self.assertEqual(lisppy.LispyVM().execute("(+ 2 3)").value, 5)
        self.assertIsInstance(InMemoryIdempotencyStore(), InMemoryIdempotencyStore)

    def test_python_module_entrypoint(self):
        result = run_module("-e", "(identity 7)")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "7\n")

    def test_installed_demo_entrypoint_contract(self):
        result = run_python("-m", "lisppy.demo")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            json.loads(result.stdout)["api"],
            "lispy.hosted-flow/v2",
        )
        demo = lisppy.run_demo()
        self.assertEqual(demo["api"], "lispy.hosted-flow/v2")
        self.assertTrue(demo["ok"])
        self.assertTrue(all(demo["checks"].values()))
        self.assertEqual(demo["idempotent_replay"]["adapter_calls"], 1)

    def test_demo_failure_is_one_redacted_json_line(self):
        import lisppy.demo as demo_module

        output = io.StringIO()
        with mock.patch.object(
            demo_module,
            "registered_source",
            side_effect=RuntimeError("private detail"),
        ), contextlib.redirect_stdout(output):
            status = demo_module.main([])
        self.assertEqual(status, 1)
        response = json.loads(output.getvalue())
        self.assertEqual(response["api"], "lispy.error/v1")
        self.assertEqual(response["error"]["code"], "demo_failed")
        self.assertNotIn("private", output.getvalue())

    def test_demo_semantic_failure_returns_nonzero(self):
        import lisppy.demo as demo_module

        output = io.StringIO()
        with mock.patch.object(
            demo_module,
            "run_demo",
            return_value={
                "api": "lispy.hosted-flow/v2",
                "ok": False,
                "checks": {"first_frame_committed": False},
            },
        ), contextlib.redirect_stdout(output):
            status = demo_module.main([])
        self.assertEqual(status, 1)
        self.assertFalse(json.loads(output.getvalue())["ok"])

    def test_demo_module_help_does_not_execute_the_demo(self):
        result = run_python("-m", "lisppy.demo", "--help")
        self.assertEqual(result.returncode, 0)
        self.assertIn("usage:", result.stdout)
        self.assertNotIn("lispy.hosted-flow", result.stdout)

    def test_fixture_imports_are_locale_independent(self):
        result = run_python(
            "-c",
            "import tests.test_worker, tests.test_hosted_governor; print('ok')",
            env_overrides={
                "LC_ALL": "C",
                "PYTHONUTF8": "0",
                "PYTHONCOERCECLOCALE": "0",
            },
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "ok\n")

    def test_packaged_runtime_resources_are_available(self):
        self.assertIn("(define (identity x) x)", lisp._resource_text(
            "stdlib.lisp", ROOT / "missing"
        ))
        registered = lisp._registered_sources()
        self.assertEqual(
            set(registered),
            {
                "lispy/hosted-doctor@1",
                "mars-barn/governor-example",
            },
        )
        mirrors = {
            "stdlib.lisp": ROOT / "stdlib.lisp",
            "registered/hosted-governor.lisp": ROOT / "examples" / "hosted-governor.lisp",
            "registered/mars-colony-governor.lisp": (
                ROOT / "examples" / "mars-barn" / "mars-colony-governor.lisp"
            ),
        }
        for name, mirror in mirrors.items():
            with self.subTest(resource=name):
                self.assertEqual(
                    lisp._resource_bytes(name, ROOT / "missing"),
                    mirror.read_bytes(),
                )

    def test_module_doctor_uses_packaged_resources(self):
        result = run_module("--doctor", "--json")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(json.loads(result.stdout)["ok"])

    def test_release_workflow_is_fail_closed_and_verifies_testpypi(self):
        workflow = (
            ROOT / ".github" / "workflows" / "publish.yml"
        ).read_text(encoding="utf-8")
        preflight = workflow.split("\n  build:", 1)[0]
        self.assertIn("\n  preflight:\n", preflight)
        self.assertNotIn("\n    environment:", preflight)
        self.assertIn("\n    permissions: {}\n", preflight)
        self.assertNotIn("if: vars.", workflow)
        self.assertIn("\n  build:\n    needs: preflight\n", workflow)
        self.assertIn(
            "\n  publish-testpypi:\n    needs: build\n",
            workflow,
        )
        self.assertIn(
            "\n  verify-testpypi:\n"
            "    needs: [build, publish-testpypi]\n",
            workflow,
        )
        self.assertIn(
            "\n  publish-pypi:\n"
            "    needs: [build, verify-testpypi]\n",
            workflow,
        )
        self.assertIn(
            "python .github/scripts/verify_testpypi.py",
            workflow,
        )
        self.assertNotIn("continue-on-error", workflow)
        self.assertEqual(workflow.count("skip-existing: false"), 2)

    def test_ci_doctors_wheel_and_sdist_installs(self):
        workflow = (
            ROOT / ".github" / "workflows" / "tests.yml"
        ).read_text(encoding="utf-8")
        self.assertIn("python -m pip wheel dist/*.tar.gz", workflow)
        self.assertEqual(workflow.count("--doctor release@2"), 2)
        self.assertEqual(workflow.count("--doctor-mode installed"), 2)
        self.assertEqual(workflow.count("-I -m lisppy.demo"), 2)
        self.assertEqual(workflow.count("-I -m lisppy.contracts"), 4)
        self.assertEqual(workflow.count("--verify"), 2)
        self.assertEqual(workflow.count("--expect-manifest"), 2)
        self.assertIn(
            'test "$source_pin_before" = "$source_pin_after"',
            workflow,
        )
        self.assertIn(
            'test "$build_pin_before" = "$build_pin_after"',
            workflow,
        )

    def test_build_source_identity_binds_packaging_inputs(self):
        digest = lisp.build_source_sha256()
        self.assertRegex(digest, r"^[0-9a-f]{64}$")
        self.assertEqual(
            set(lisp.BUILD_INPUT_PATHS),
            {
                "MANIFEST.in",
                "README.md",
                "pyproject.toml",
                "setup.cfg",
                "setup.py",
            },
        )

    def test_distribution_metadata_uses_public_release_name(self):
        setup = (ROOT / "setup.cfg").read_text(encoding="utf-8")
        self.assertIn("name = rappterbook-lispy-runtime", setup)

    def test_installed_record_rejects_unexpected_executable_payloads(self):
        def row(relative):
            content = (ROOT / relative).read_bytes()
            digest = base64.urlsafe_b64encode(
                hashlib.sha256(content).digest()
            ).decode("ascii").rstrip("=")
            return f"{relative},sha256={digest},{len(content)}"

        class Distribution:
            def __init__(self, record):
                self.record = record

            def read_text(self, name):
                return self.record if name == "RECORD" else None

            def locate_file(self, relative):
                return ROOT / relative

        rows = [row(relative) for relative in lisp.INSTALLED_REQUIRED_PATHS]
        artifacts = lisp._verify_distribution_record(
            Distribution("\n".join(rows))
        )
        self.assertEqual(
            {item["path"] for item in artifacts},
            set(lisp.INSTALLED_REQUIRED_PATHS),
        )
        evil = rows + ["lisppy/extra_payload.py,sha256=AAAA,1"]
        with self.assertRaisesRegex(
            lisp.InvalidDataError,
            "unexpected installed path",
        ):
            lisp._verify_distribution_record(
                Distribution("\n".join(evil))
            )
        for license_path in (
            f"rappterbook_lispy_runtime-{lisp.VERSION}.dist-info/LICENSE",
            f"rappterbook_lispy_runtime-{lisp.VERSION}.dist-info/licenses/LICENSE",
        ):
            with self.subTest(license_path=license_path):
                lisp._validate_distribution_record_paths(
                    {
                        **{
                            relative: ("hash", "1")
                            for relative in lisp.INSTALLED_REQUIRED_PATHS
                        },
                        license_path: ("hash", "1"),
                    }
                )
        with self.assertRaisesRegex(
            lisp.InvalidDataError,
            "unexpected installed path",
        ):
            lisp._validate_distribution_record_paths(
                {
                    **{
                        relative: ("hash", "1")
                        for relative in lisp.INSTALLED_REQUIRED_PATHS
                    },
                    "attacker/bin/lispy": ("sha256=AAAA", "1"),
                }
            )

    def test_release_text_is_valid_and_truthful(self):
        for relative in ("README.md", "LISPY.md", "CHANGELOG.md"):
            with self.subTest(path=relative):
                text = (ROOT / relative).read_text(encoding="utf-8")
                self.assertNotIn("\ufffd", text)
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn(
            "The host remains\nresponsible for validation",
            readme,
        )
        self.assertIn("external and unverified", readme.lower())


if __name__ == "__main__":
    unittest.main()
