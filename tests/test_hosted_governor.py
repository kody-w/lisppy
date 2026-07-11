import contextlib
import hashlib
import io
import json
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import lisp


GOVERNOR = (
    ROOT / "examples" / "mars-barn" / "mars-colony-governor.lisp"
).read_text(encoding="utf-8")
INPUTS = {
    "sol": 1,
    "o2_days": 3.0,
    "h2o_days": 20.0,
    "food_days": 20.0,
    "power_kwh": 200.0,
    "colony_risk_index": 10.0,
}
OUTPUTS = {
    "heating_alloc": 0.25,
    "isru_alloc": 0.40,
    "greenhouse_alloc": 0.35,
    "food_ration": 1.0,
}


class HostedGovernorTests(unittest.TestCase):
    def run_governor(self, source=GOVERNOR, **kwargs):
        inputs = kwargs.pop("inputs", INPUTS)
        outputs = kwargs.pop("mutable_outputs", OUTPUTS)
        outside = io.StringIO()
        with contextlib.redirect_stdout(outside):
            receipt = lisp.run_hosted_governor(
                source,
                inputs=inputs,
                mutable_outputs=outputs,
                contract_id="mars-barn/governor-controls@1",
                **kwargs,
            )
        self.assertEqual(outside.getvalue(), "")
        json.dumps(receipt, allow_nan=False)
        return receipt

    def test_emergency_branch_returns_transactional_receipt(self):
        receipt = self.run_governor()
        self.assertEqual(receipt["status"], "accepted")
        self.assertEqual(
            receipt["source_sha256"],
            hashlib.sha256(GOVERNOR.encode("utf-8")).hexdigest(),
        )
        self.assertEqual(receipt["outputs"]["isru_alloc"], 0.85)
        self.assertEqual(receipt["outputs"]["greenhouse_alloc"], 0.05)
        self.assertEqual(receipt["outputs"]["heating_alloc"], 0.10)
        self.assertEqual(receipt["outputs"]["food_ration"], 0.5)
        self.assertEqual(
            receipt["writes"],
            [
                "food_ration",
                "greenhouse_alloc",
                "heating_alloc",
                "isru_alloc",
            ],
        )
        self.assertIn("O2 EMERGENCY", receipt["logs"].replace("₂", "2"))

    def test_policy_writes_all_controls_in_every_branch(self):
        cases = [
            (
                {"o2_days": 20.0, "h2o_days": 3.0},
                (0.12, 0.80, 0.08, 0.55),
            ),
            (
                {"o2_days": 20.0, "food_days": 5.0},
                (0.15, 0.25, 0.60, 0.65),
            ),
            (
                {"o2_days": 20.0, "power_kwh": 50.0},
                (0.55, 0.25, 0.20, 0.75),
            ),
            (
                {"o2_days": 20.0, "colony_risk_index": 60.0},
                (0.40, 0.35, 0.25, 0.80),
            ),
            (
                {"o2_days": 20.0},
                (0.25, 0.35, 0.40, 1.0),
            ),
        ]
        for changes, expected in cases:
            with self.subTest(changes=changes):
                inputs = {**INPUTS, **changes}
                receipt = self.run_governor(inputs=inputs)
                outputs = receipt["outputs"]
                self.assertEqual(
                    (
                        outputs["heating_alloc"],
                        outputs["isru_alloc"],
                        outputs["greenhouse_alloc"],
                        outputs["food_ration"],
                    ),
                    expected,
                )
                self.assertEqual(
                    receipt["writes"],
                    [
                        "food_ration",
                        "greenhouse_alloc",
                        "heating_alloc",
                        "isru_alloc",
                    ],
                )

    def test_partial_writes_are_rolled_back_after_error(self):
        receipt = self.run_governor(GOVERNOR + '\n(error "boom")')
        self.assertEqual(receipt["status"], "rolled_back")
        self.assertIsNone(receipt["outputs"])
        self.assertEqual(receipt["writes"], [])
        self.assertEqual(receipt["error"]["phase"], "evaluate")
        self.assertEqual(INPUTS["sol"], 1)
        self.assertEqual(OUTPUTS["isru_alloc"], 0.40)

    def test_inputs_and_runtime_bindings_are_read_only(self):
        input_receipt = self.run_governor("(set! sol 99)")
        builtin_receipt = self.run_governor("(set! + 99)")
        self.assertEqual(input_receipt["status"], "rolled_back")
        self.assertIn("read-only binding: sol", input_receipt["error"]["message"])
        self.assertEqual(builtin_receipt["status"], "rolled_back")
        self.assertIn("read-only binding: +", builtin_receipt["error"]["message"])

    def test_cyclic_host_input_is_rejected(self):
        cyclic = {}
        cyclic["self"] = cyclic
        receipt = lisp.run_hosted_governor(
            "1",
            inputs=cyclic,
            mutable_outputs=OUTPUTS,
        )
        self.assertEqual(receipt["status"], "rolled_back")
        self.assertEqual(receipt["error"]["phase"], "input")
        self.assertIn("cyclic host value", receipt["error"]["message"])

    def test_local_shadow_does_not_create_a_host_write(self):
        receipt = self.run_governor(
            "((lambda (heating_alloc) (set! heating_alloc 0.9)) 0.5)"
        )
        self.assertEqual(receipt["status"], "accepted")
        self.assertEqual(receipt["outputs"]["heating_alloc"], 0.25)
        self.assertEqual(receipt["writes"], [])

    def test_invalid_host_name_and_validator_error_roll_back(self):
        invalid = lisp.run_hosted_governor(
            "1",
            inputs={"bad key": 1},
            mutable_outputs=OUTPUTS,
        )
        self.assertEqual(invalid["status"], "rolled_back")
        self.assertEqual(invalid["error"]["phase"], "input")

        def broken_validator(_inputs, _outputs):
            raise RuntimeError("validator unavailable")

        broken = self.run_governor("1", validate=broken_validator)
        self.assertEqual(broken["status"], "rolled_back")
        self.assertEqual(broken["error"]["phase"], "validate")
        self.assertIn("validator unavailable", broken["error"]["message"])

        def attribute_validator(_inputs, _outputs):
            raise AttributeError("validator attribute missing")

        attribute = self.run_governor("1", validate=attribute_validator)
        self.assertEqual(attribute["status"], "rolled_back")
        self.assertEqual(attribute["error"]["phase"], "validate")

    def test_host_validation_can_reject_candidate(self):
        def validate(_inputs, outputs):
            total = sum(
                outputs[name]
                for name in (
                    "heating_alloc",
                    "isru_alloc",
                    "greenhouse_alloc",
                )
            )
            return [] if abs(total - 1.0) < 1e-9 else ["allocations must sum to 1"]

        receipt = self.run_governor(
            "(set! isru_alloc 2)",
            validate=validate,
        )
        self.assertEqual(receipt["status"], "rolled_back")
        self.assertEqual(receipt["error"]["phase"], "validate")
        self.assertIn("allocations must sum to 1", receipt["error"]["message"])


if __name__ == "__main__":
    unittest.main()
