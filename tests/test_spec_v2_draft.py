import json
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SPEC = ROOT / "spec" / "v2"


class Core2DraftTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.model = json.loads(
            (SPEC / "model.json").read_text(encoding="utf-8")
        )

    def test_exact_inert_layout(self):
        self.assertEqual(
            sorted(path.name for path in SPEC.iterdir()),
            ["README.md", "model.json"],
        )

    def test_draft_has_no_runtime_claim(self):
        self.assertEqual(self.model["schema"], "lispy.inert-design/v1")
        self.assertEqual(self.model["status"], "draft")
        self.assertIs(self.model["inert"], True)
        self.assertIs(self.model["normative"], False)
        self.assertEqual(self.model["runtime_claims"], [])

    def test_form_vector_boundary_is_consistent(self):
        execution = self.model["execution"]
        form = self.model["types"]["Form"]
        vector = self.model["types"]["Vector"]
        self.assertEqual(execution["dispatch_kind"], "Form")
        self.assertFalse(execution["implicit_sequence_to_form"])
        self.assertTrue(form["executable"])
        self.assertFalse(vector["executable"])
        self.assertEqual(form["reader_delimiters"], ["(", ")"])
        self.assertEqual(vector["reader_delimiters"], ["[", "]"])
        self.assertEqual(self.model["json"]["decode"]["array"], "Vector")
        self.assertIn(
            ["Form", "Vector"],
            self.model["equality"]["distinct_kind_pairs"],
        )

    def test_worker_selection_is_named_and_unassigned(self):
        self.assertEqual(
            self.model["worker"]["selection_fields"],
            {
                "worker_api_id": None,
                "operation_id": None,
                "language_profile": None,
                "contract_id": None,
            },
        )
        self.assertEqual(
            self.model["worker"]["selection_assignment"],
            "unassigned",
        )
        self.assertEqual(self.model["worker"]["operations_added"], [])
        self.assertFalse(self.model["worker"]["client_data_execution"])

    def test_readme_explicitly_disclaims_implementation(self):
        readme = (SPEC / "README.md").read_text(encoding="utf-8")
        self.assertIn("inert, non-normative", readme)
        self.assertIn("implemented runtime\nremains `lispy-core@1`", readme)
        self.assertNotIn("```", readme)


if __name__ == "__main__":
    unittest.main()
