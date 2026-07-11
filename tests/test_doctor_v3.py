import copy
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import lisp


class DoctorV3ContractTests(unittest.TestCase):
    def test_source_inventory_report_is_strict_and_valid(self):
        report = lisp._doctor_v3("inventory@1", "source", None)
        self.assertTrue(report["ok"])
        self.assertIs(lisp._validate_doctor_v3_report(report), report)
        self.assertEqual(
            [check["id"] for check in report["components"]["inventory"]["checks"]],
            list(lisp.DOCTOR_V3_CHECK_CATALOGS["inventory"]["source"]),
        )

    def test_pin_mismatch_remains_a_schema_valid_failed_report(self):
        report = lisp._doctor_v3("inventory@1", "source", "0" * 64)
        self.assertFalse(report["ok"])
        self.assertFalse(report["components"]["inventory"]["ok"])
        self.assertTrue(report["artifacts"])
        self.assertEqual(
            report["components"]["inventory"]["checks"][-1]["error"]["code"],
            "inventory_pin_mismatch",
        )
        self.assertIs(lisp._validate_doctor_v3_report(report), report)

    def test_failed_installed_checks_keep_canonical_inventory_evidence(self):
        check_ids = lisp.DOCTOR_V3_CHECK_CATALOGS["inventory"]["installed"][:-1]
        checks = [
            lisp._doctor_check(
                check_id,
                False,
                error={"category": "package", "code": "simulated_failure"},
            )
            for check_id in check_ids
        ]
        with mock.patch.object(
            lisp,
            "_installed_doctor_profile",
            return_value=(checks, []),
        ):
            report = lisp._doctor_v3("inventory@1", "installed", None)
        self.assertFalse(report["ok"])
        self.assertEqual(report["artifacts"], [])
        self.assertIn(
            "inventory",
            report["components"]["inventory"]["evidence"],
        )
        self.assertIs(lisp._validate_doctor_v3_report(report), report)

    def test_component_exceptions_are_isolated(self):
        with mock.patch.object(
            lisp,
            "_collect_replay_evidence",
            side_effect=StopIteration(),
        ):
            replay = lisp._doctor_v3("replay@2", "source", None)
        self.assertFalse(replay["ok"])
        self.assertFalse(replay["components"]["replay"]["ok"])
        self.assertTrue(replay["components"]["inventory"]["ok"])

        with mock.patch.object(
            lisp,
            "_effects_doctor_profile",
            side_effect=subprocess.TimeoutExpired("doctor", 1),
        ):
            effects = lisp._doctor_v3("effects@2", "source", None)
        self.assertFalse(effects["ok"])
        self.assertFalse(effects["components"]["effects"]["ok"])
        self.assertTrue(effects["components"]["inventory"]["ok"])

    def test_validator_rejects_catalog_identity_and_error_mutations(self):
        report = lisp._doctor_v3("inventory@1", "source", None)

        bad_catalog = copy.deepcopy(report)
        bad_catalog["components"]["inventory"]["checks"][0]["id"] = "invented"
        with self.assertRaises(lisp.InvalidDataError):
            lisp._validate_doctor_v3_report(bad_catalog)

        bad_identity = copy.deepcopy(report)
        bad_identity["components"]["inventory"]["evidence"]["inventory"][
            "distribution"
        ] = "evil"
        with self.assertRaises(lisp.InvalidDataError):
            lisp._validate_doctor_v3_report(bad_identity)

        failed = lisp._doctor_v3("inventory@1", "source", "0" * 64)
        failed["error"] = "component_failure"
        with self.assertRaises(lisp.InvalidDataError):
            lisp._validate_doctor_v3_report(failed)

        bad_profile = copy.deepcopy(report)
        bad_profile["profile"] = []
        with self.assertRaises(lisp.InvalidDataError):
            lisp._validate_doctor_v3_report(bad_profile)

    def test_replay_evidence_must_match_check_results(self):
        report = lisp._doctor_v3("replay@2", "source", None)
        replay = report["components"]["replay"]
        replay["evidence"]["state_after_sha256"] = "0" * 64
        with self.assertRaises(lisp.InvalidDataError):
            lisp._validate_doctor_v3_report(report)

    def test_effects_evidence_digest_is_recomputed(self):
        report = lisp._doctor_v3("effects@2", "source", None)
        self.assertTrue(report["ok"])
        report["components"]["effects"]["evidence"]["checks_sha256"] = "0" * 64
        with self.assertRaises(lisp.InvalidDataError):
            lisp._validate_doctor_v3_report(report)

    def test_non_string_pin_is_rejected_as_invalid_data(self):
        report = lisp._doctor_v3("inventory@1", "source", None)
        report["pins"]["inventory_sha256"] = 1
        with self.assertRaises(lisp.InvalidDataError):
            lisp._validate_doctor_v3_report(report)


if __name__ == "__main__":
    unittest.main()
