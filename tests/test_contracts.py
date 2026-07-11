import copy
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from lisppy import (
    contract_bundle,
    contract_bundle_v2,
    contract_manifest,
    load_contract,
    load_profile,
    verify_contract_bundle_v2,
)
from tests.support import run_python


def replace_resource(bundle, path, content):
    value = copy.deepcopy(bundle)
    encoded = content.encode("utf-8")
    for resource in value["resources"]:
        if resource["path"] == path:
            resource["content"] = content
            resource["size"] = len(encoded)
            resource["sha256"] = hashlib.sha256(encoded).hexdigest()
            break
    value["manifest"]["files"] = [
        {
            "path": resource["path"],
            "size": resource["size"],
            "sha256": resource["sha256"],
        }
        for resource in value["resources"]
    ]
    payload = {
        key: value["manifest"][key]
        for key in (
            "api",
            "profile",
            "schema",
            "wire",
            "case_count",
            "files",
        )
    }
    value["manifest"]["sha256"] = hashlib.sha256(
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    return value


class InstalledContractTests(unittest.TestCase):
    def test_source_and_packaged_contract_resources_match(self):
        mirrors = {
            "CONFORMANCE.md": ROOT / "spec" / "v1" / "CONFORMANCE.md",
            "README.md": ROOT / "spec" / "v1" / "README.md",
            "conformance.json": ROOT / "spec" / "v1" / "conformance.json",
            "profile.json": ROOT / "spec" / "v1" / "profile.json",
            "stdlib.lisp": ROOT / "spec" / "v1" / "stdlib.lisp",
        }
        packaged_root = (
            ROOT / "lisppy" / "data" / "contracts" / "lispy-core@1"
        )
        for name, source in mirrors.items():
            with self.subTest(name=name):
                self.assertEqual(
                    (packaged_root / name).read_bytes(),
                    source.read_bytes(),
                )

    def test_manifest_binds_every_resource_and_case(self):
        manifest = contract_manifest()
        self.assertEqual(manifest["api"], "lispy.contract-manifest/v1")
        self.assertEqual(manifest["profile"], "lispy-core@1")
        self.assertEqual(manifest["case_count"], len(load_contract()["cases"]))
        packaged_root = (
            ROOT / "lisppy" / "data" / "contracts" / "lispy-core@1"
        )
        for item in manifest["files"]:
            content = (packaged_root / item["path"]).read_bytes()
            self.assertEqual(item["size"], len(content))
            self.assertEqual(
                item["sha256"],
                hashlib.sha256(content).hexdigest(),
            )

    def test_contract_pack_export_is_deterministic(self):
        first = run_python("-m", "lisppy.contracts")
        second = run_python("-m", "lisppy.contracts")
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertEqual(first.stdout, second.stdout)
        exported = json.loads(first.stdout)
        self.assertEqual(exported, contract_bundle_v2())
        self.assertEqual(exported["api"], "lispy.contract-pack/v2")
        for resource in exported["resources"]:
            content = resource["content"].encode(resource["encoding"])
            self.assertEqual(len(content), resource["size"])
            self.assertEqual(
                hashlib.sha256(content).hexdigest(),
                resource["sha256"],
            )
        legacy = run_python(
            "-m",
            "lisppy.contracts",
            "--format",
            "v1",
        )
        self.assertEqual(json.loads(legacy.stdout), contract_bundle())
        self.assertEqual(
            exported["manifest"]["case_count"],
            len(load_contract()["cases"]),
        )

    def test_unknown_contract_is_rejected(self):
        with self.assertRaises(ValueError):
            load_contract("unknown")

    def test_profile_resource_is_strict_and_consistent(self):
        profile = load_profile()
        self.assertEqual(profile["profile"], "lispy-core@1")
        self.assertEqual(profile["corpus"], load_contract()["schema"])
        self.assertEqual(
            profile["stdlib"]["exports"],
            ["identity", "constantly", "complement", "partial"],
        )
        self.assertEqual(profile["host_extensions"], "excluded")

    def test_core_stdlib_has_no_host_profile_dependencies(self):
        core = (
            ROOT / "spec" / "v1" / "stdlib.lisp"
        ).read_text(encoding="utf-8")
        extension = (
            ROOT / "rappterbook-stdlib.lisp"
        ).read_text(encoding="utf-8")
        aggregate = (ROOT / "stdlib.lisp").read_text(encoding="utf-8")
        self.assertNotIn("(rb-", core)
        self.assertIn("(rb-state", extension)
        for name in ("identity", "constantly", "complement", "partial"):
            self.assertIn(f"(define ({name}", core)
            self.assertIn(f"(define ({name}", aggregate)

    def test_module_help_does_not_export_a_contract(self):
        result = run_python("-m", "lisppy.contracts", "--help")
        self.assertEqual(result.returncode, 0)
        self.assertIn("usage:", result.stdout)
        self.assertNotIn("lispy.contract-pack", result.stdout)

    def test_public_verifier_rejects_tampered_or_open_packs(self):
        valid = contract_bundle_v2()
        self.assertIs(verify_contract_bundle_v2(valid), valid)
        mutations = []
        content = copy.deepcopy(valid)
        content["resources"][0]["content"] += "tamper"
        mutations.append(content)
        digest = copy.deepcopy(valid)
        digest["manifest"]["sha256"] = "0" * 64
        mutations.append(digest)
        missing = copy.deepcopy(valid)
        missing["resources"].pop()
        mutations.append(missing)
        duplicate = copy.deepcopy(valid)
        duplicate["resources"][1]["path"] = duplicate["resources"][0]["path"]
        mutations.append(duplicate)
        for bundle in mutations:
            with self.subTest(bundle=bundle):
                with self.assertRaises(ValueError):
                    verify_contract_bundle_v2(bundle)

    def test_module_verifies_exported_pack(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "contract.json"
            path.write_text(
                json.dumps(contract_bundle_v2()),
                encoding="utf-8",
            )
            result = run_python(
                "-m",
                "lisppy.contracts",
                "--verify",
                str(path),
            )
        self.assertEqual(result.returncode, 0, result.stderr)
        response = json.loads(result.stdout)
        self.assertTrue(response["ok"])
        self.assertEqual(
            response["manifest_sha256"],
            contract_manifest()["sha256"],
        )

    def test_semantically_invalid_rehashed_corpora_are_rejected(self):
        valid = contract_bundle_v2()
        corpus = load_contract()
        invalid_values = [
            [],
            {**corpus, "cases": []},
            {
                **corpus,
                "cases": [
                    {
                        **corpus["cases"][0],
                        "expect": {
                            "stdout": "",
                            "value": {"tag": "nil"},
                            "error": {"category": "evaluation"},
                        },
                    }
                ],
            },
            {
                **corpus,
                "cases": [corpus["cases"][0], corpus["cases"][0]],
            },
        ]
        for corpus_value in invalid_values:
            with self.subTest(corpus=corpus_value):
                bundle = replace_resource(
                    valid,
                    "conformance.json",
                    json.dumps(corpus_value),
                )
                bundle["manifest"]["case_count"] = (
                    len(corpus_value.get("cases", []))
                    if isinstance(corpus_value, dict)
                    else 0
                )
                payload = {
                    key: bundle["manifest"][key]
                    for key in (
                        "api",
                        "profile",
                        "schema",
                        "wire",
                        "case_count",
                        "files",
                    )
                }
                bundle["manifest"]["sha256"] = hashlib.sha256(
                    json.dumps(
                        payload,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ).encode("utf-8")
                ).hexdigest()
                with self.assertRaises(ValueError):
                    verify_contract_bundle_v2(bundle)

    def test_external_manifest_pin_is_enforced_by_api_and_cli(self):
        bundle = contract_bundle_v2()
        with self.assertRaisesRegex(ValueError, "external"):
            verify_contract_bundle_v2(
                bundle,
                expected_manifest_sha256="0" * 64,
            )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "contract.json"
            path.write_text(json.dumps(bundle), encoding="utf-8")
            rejected = run_python(
                "-m",
                "lisppy.contracts",
                "--verify",
                str(path),
                "--expect-manifest",
                "0" * 64,
            )
            accepted = run_python(
                "-m",
                "lisppy.contracts",
                "--verify",
                str(path),
                "--expect-manifest",
                bundle["manifest"]["sha256"],
            )
        self.assertEqual(rejected.returncode, 1)
        self.assertEqual(
            json.loads(rejected.stdout)["error"]["code"],
            "contract_verification_failed",
        )
        self.assertEqual(accepted.returncode, 0, accepted.stderr)

    def test_contract_export_is_ascii_safe_in_a_c_locale(self):
        environment = os.environ.copy()
        environment.pop("PYTHONIOENCODING", None)
        environment.update(
            {
                "LC_ALL": "C",
                "PYTHONUTF8": "0",
                "PYTHONCOERCECLOCALE": "0",
            }
        )
        result = subprocess.run(
            [sys.executable, "-m", "lisppy.contracts"],
            capture_output=True,
            cwd=ROOT,
            env=environment,
            timeout=20,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        result.stdout.decode("ascii")


if __name__ == "__main__":
    unittest.main()
