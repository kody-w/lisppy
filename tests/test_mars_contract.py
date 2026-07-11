import hashlib
import json
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import lisp
from lisppy.mars import (
    load_mars_contract,
    load_mars_vectors,
    run_mars_vectors,
)
from tests.support import run_python


class MarsContractTests(unittest.TestCase):
    def test_source_and_packaged_mars_evidence_match(self):
        mirrors = {
            "governor-contract.json": (
                ROOT / "spec" / "mars" / "governor-contract.json"
            ),
            "governor-vectors.json": (
                ROOT / "spec" / "mars" / "governor-vectors.json"
            ),
        }
        package_root = ROOT / "lisppy" / "data" / "mars"
        for name, source in mirrors.items():
            with self.subTest(name=name):
                self.assertEqual(
                    (package_root / name).read_bytes(),
                    source.read_bytes(),
                )

    def test_contract_binds_registered_source_and_decision_only_policy(self):
        contract = load_mars_contract()
        source = lisp.registered_source(contract["source_id"])
        self.assertEqual(contract["source_sha256"], source["source_sha256"])
        self.assertEqual(contract["profile"], source["profile"])
        self.assertEqual(contract["contract_id"], source["contract_id"])
        self.assertEqual(contract["effect_types"], [])
        self.assertEqual(contract["execution_kind"], "decision_only")
        self.assertEqual(contract["external_target_status"], "unverified")

    def test_all_packaged_policy_vectors_pass(self):
        vectors = load_mars_vectors()
        report = run_mars_vectors()
        self.assertTrue(report["ok"])
        self.assertEqual(report["summary"]["total"], len(vectors["vectors"]))
        self.assertEqual(
            report["summary"]["passed"],
            report["summary"]["total"],
        )
        self.assertTrue(all(result["ok"] for result in report["results"]))
        self.assertEqual(report["external_target_status"], "unverified")
        self.assertEqual(
            report["contract_sha256"],
            hashlib.sha256(
                (
                    ROOT
                    / "lisppy"
                    / "data"
                    / "mars"
                    / "governor-contract.json"
                ).read_bytes()
            ).hexdigest(),
        )

    def test_mars_evidence_cli_is_deterministic(self):
        first = run_python("-m", "lisppy.mars")
        second = run_python("-m", "lisppy.mars")
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertEqual(first.stdout, second.stdout)
        report = json.loads(first.stdout)
        self.assertTrue(report["ok"])
        self.assertEqual(report["api"], "lispy.mars-policy-evidence/v1")


if __name__ == "__main__":
    unittest.main()
