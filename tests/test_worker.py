import hashlib
import json
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import lisp
from tests.support import fresh_replay_bundle, run_cli


GOVERNOR = (
    ROOT / "examples" / "mars-barn" / "mars-colony-governor.lisp"
).read_text(encoding="utf-8")


def request(request_id, operation, **values):
    return {
        "api": "lispy.worker/v1",
        "id": request_id,
        "op": operation,
        **values,
    }


class WorkerTests(unittest.TestCase):
    def run_batch(self, lines):
        result = run_cli(
            "--jsonl",
            stdin="\n".join(lines) + "\n",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, "")
        responses = [json.loads(line) for line in result.stdout.splitlines()]
        self.assertEqual(len(responses), len(lines))
        return responses

    def governor_request(
        self,
        request_id="governor-1",
        source_id="mars-barn/governor-example",
    ):
        return request(
            request_id,
            "hosted-governor",
            source_id=source_id,
            expected_source_sha256=hashlib.sha256(
                lisp._registered_sources()[source_id]["source"].encode("utf-8")
            ).hexdigest(),
            inputs={
                "sol": 1,
                "o2_days": 3.0,
                "h2o_days": 20.0,
                "food_days": 20.0,
                "power_kwh": 200.0,
                "colony_risk_index": 10.0,
            },
            mutable_outputs={
                "heating_alloc": 0.25,
                "isru_alloc": 0.40,
                "greenhouse_alloc": 0.35,
                "food_ration": 1.0,
            },
            contract_id="mars-barn/governor-controls@1",
        )

    def test_mixed_jsonl_batch_stays_framed(self):
        lines = [
            json.dumps(request("manifest-1", "manifest")),
            "{",
            json.dumps(self.governor_request()),
        ]
        manifest, malformed, governor = self.run_batch(lines)
        self.assertTrue(manifest["ok"])
        self.assertEqual(manifest["id"], "manifest-1")
        self.assertIn("hosted-governor", manifest["manifest"]["operations"])
        self.assertFalse(malformed["ok"])
        self.assertEqual(malformed["error"]["code"], "invalid_json")
        self.assertTrue(governor["ok"])
        self.assertEqual(governor["receipt"]["status"], "accepted")
        self.assertEqual(governor["receipt"]["outputs"]["isru_alloc"], 0.85)
        self.assertIn("O₂ EMERGENCY", governor["receipt"]["logs"])

    def test_source_hash_mismatch_is_rejected_before_execution(self):
        payload = self.governor_request("hash-mismatch")
        payload["expected_source_sha256"] = "0" * 64
        response = self.run_batch([json.dumps(payload)])[0]
        self.assertFalse(response["ok"])
        self.assertEqual(response["id"], "hash-mismatch")
        self.assertIn("SHA-256 mismatch", response["error"]["message"])

    def test_source_hash_is_mandatory(self):
        payload = self.governor_request("hash-required")
        payload.pop("expected_source_sha256")
        response = self.run_batch([json.dumps(payload)])[0]
        self.assertFalse(response["ok"])
        self.assertIn("missing fields", response["error"]["message"])

        payload = self.governor_request("hash-null")
        payload["expected_source_sha256"] = None
        response = self.run_batch([json.dumps(payload)])[0]
        self.assertFalse(response["ok"])
        self.assertIn("must be lowercase SHA-256", response["error"]["message"])

    def test_worker_captures_effects_without_protocol_noise(self):
        payload = self.governor_request(
            "effects-1",
            "lispy/hosted-doctor@1",
        )
        payload["intent_scope"] = "sol-1"
        response = self.run_batch([json.dumps(payload)])[0]
        self.assertTrue(response["ok"])
        receipt = response["receipt"]
        self.assertEqual(receipt["status"], "accepted")
        self.assertEqual(len(receipt["effects"]), 1)
        self.assertEqual(
            receipt["effects"][0]["type"],
            "rappterbook.post.create",
        )
        self.assertEqual(receipt["effects"][0]["mode"], "dry_run")
        self.assertFalse(receipt["effects"][0]["applied"])

    def test_registered_contract_rejects_inline_source(self):
        payload = self.governor_request("inline-rejected")
        payload.pop("source_id")
        payload["source"] = GOVERNOR
        response = self.run_batch([json.dumps(payload)])[0]
        self.assertFalse(response["ok"])
        self.assertIn("source_id is required", response["error"]["message"])

    def test_wrong_field_shapes_fail_without_worker_crash(self):
        payload = self.governor_request("bad-shape")
        payload["inputs"] = []
        response = self.run_batch([json.dumps(payload)])[0]
        self.assertFalse(response["ok"])
        self.assertEqual(response["id"], "bad-shape")
        self.assertEqual(response["error"]["code"], "invalid_request")
        self.assertNotIn("Traceback", response["error"]["message"])

    def test_oversized_line_is_drained_and_next_request_recovers(self):
        oversized = "x" * (2_097_152 + 1)
        manifest = json.dumps(request("after-oversize", "manifest"))
        first, second = self.run_batch([oversized, manifest])
        self.assertEqual(first["error"]["code"], "line_too_large")
        self.assertTrue(second["ok"])
        self.assertEqual(second["id"], "after-oversize")

    def test_doctor_proves_replay_and_state_isolation(self):
        first = run_cli("--doctor", "--json")
        second = run_cli("--doctor", "--json")
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertEqual(first.stderr, "")
        self.assertEqual(first.stdout, second.stdout)
        report = json.loads(first.stdout)
        self.assertTrue(report["ok"])
        self.assertTrue(all(report["checks"].values()))
        self.assertEqual(
            report["receipt"]["source_id"],
            "lispy/hosted-doctor@1",
        )

    def test_replay_bundle_round_trip_and_tamper_detection(self):
        with fresh_replay_bundle() as bundle:
            replay = run_cli(
                "--replay",
                str(bundle),
                "--json",
            )
            self.assertEqual(replay.returncode, 0, replay.stderr)
            self.assertTrue(json.loads(replay.stdout)["ok"])

            tampered = json.loads(bundle.read_text(encoding="utf-8"))
            tampered["request"]["inputs"]["sol"] = 2
            bundle.write_text(json.dumps(tampered), encoding="utf-8")
            rejected = run_cli(
                "--replay",
                str(bundle),
                "--json",
            )
            self.assertEqual(rejected.returncode, 1)
            self.assertEqual(rejected.stderr, "")
            self.assertIn(
                "digest mismatch",
                json.loads(rejected.stdout)["error"]["message"],
            )

    def test_rehashed_runtime_mismatch_fails_closed(self):
        with fresh_replay_bundle() as bundle:
            data = json.loads(bundle.read_text(encoding="utf-8"))
            data["runtime_version"] = "tampered-version"
            payload = {
                key: value
                for key, value in data.items()
                if key != "bundle_sha256"
            }
            data["bundle_sha256"] = lisp._canonical_sha256(payload)
            bundle.write_text(json.dumps(data), encoding="utf-8")
            replay = run_cli(
                "--replay",
                str(bundle),
                "--json",
            )
        self.assertEqual(replay.returncode, 1)
        report = json.loads(replay.stdout)
        self.assertFalse(report["checks"]["runtime_match"])

    def test_replay_errors_are_structured_without_traceback(self):
        result = run_cli(
            "--replay",
            "README.md",
            "--json",
        )
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stderr, "")
        response = json.loads(result.stdout)
        self.assertFalse(response["ok"])
        self.assertNotIn("Traceback", result.stdout)


if __name__ == "__main__":
    unittest.main()
