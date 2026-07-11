import json
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import lisp
from tests.support import run_cli, run_python


class ExampleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.manifest = json.loads(
            (ROOT / "examples" / "manifest.json").read_text(encoding="utf-8")
        )

    def test_manifest_covers_every_lisp_example(self):
        discovered = {
            str(path.relative_to(ROOT))
            for path in (ROOT / "examples").rglob("*.lisp")
        }
        discovered.add("examples/hosted-flow.py")
        declared = {item["path"] for item in self.manifest["examples"]}
        self.assertEqual(declared, discovered)

    def test_manifest_profiles_are_closed_and_evidence_bearing(self):
        self.assertEqual(self.manifest["schema"], "lispy-examples@2")
        levels = set(self.manifest["proof_levels"])
        self.assertEqual(
            levels,
            {
                "local-executed",
                "external-unverified",
            },
        )
        profiles = self.manifest["profiles"]
        paths = []
        for item in self.manifest["examples"]:
            paths.append(item["path"])
            self.assertIn(item["profile"], profiles)
            self.assertEqual(item["runtime"], profiles[item["profile"]]["runtime"])
            self.assertEqual(item["proof"], profiles[item["profile"]]["proof"])
            self.assertIn(item["proof"], levels)
        self.assertEqual(len(paths), len(set(paths)))

    def test_python_examples_run_deterministically(self):
        expected = {
            "examples/hello.lisp": "Hello from LisPy: 6",
            "examples/agent-profile.lisp": "Soul: Socrates 2.0",
            "examples/channel-stats.lisp": "Total: 2 channels, 18 posts",
            "examples/data-slosh.lisp": "The REPL is the Heartbeat",
            "examples/frame-eval.lisp": "Frame complete",
            "examples/trending.lisp": "2 trending posts total",
        }
        for item in self.manifest["examples"]:
            if item["runtime"] != "python-cli":
                continue
            with self.subTest(example=item["path"]):
                args = []
                if "state_dir" in item:
                    args.extend(["--state-dir", item["state_dir"]])
                args.append(item["path"])
                first = run_cli(*args)
                second = run_cli(*args)
                self.assertEqual(first.returncode, 0, first.stderr)
                self.assertEqual(first.stderr, "")
                self.assertEqual(first.stdout, second.stdout)
                self.assertIn(expected[item["path"]], first.stdout)

    def test_offline_hosted_flow_runs_deterministically(self):
        first = run_python("examples/hosted-flow.py")
        second = run_python("examples/hosted-flow.py")
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertEqual(first.stdout, second.stdout)
        report = json.loads(first.stdout)
        self.assertEqual(report["api"], "lispy.hosted-flow/v2")
        self.assertEqual(
            report["first_frame"]["effect_status"],
            "applied",
        )
        self.assertEqual(
            report["idempotent_replay"]["effect_status"],
            "duplicate_applied",
        )
        self.assertEqual(report["idempotent_replay"]["adapter_calls"], 1)
        self.assertEqual(report["second_frame"]["status"], "committed")
        self.assertEqual(
            report["second_frame"]["initial_outputs_from_first_frame"],
            report["first_frame"]["outputs"],
        )
        self.assertNotEqual(
            report["second_frame"]["outputs"],
            report["first_frame"]["outputs"],
        )

    def test_mars_examples_are_parseable_and_profile_tagged(self):
        mars = [
            item
            for item in self.manifest["examples"]
            if item["path"].startswith("examples/mars-barn/")
        ]
        self.assertEqual(len(mars), 3)
        for item in mars:
            with self.subTest(example=item["path"]):
                expressions = lisp.parse(
                    (ROOT / item["path"]).read_text(encoding="utf-8")
                )
                self.assertTrue(expressions)
        external = [item for item in mars if item["proof"] == "external-unverified"]
        self.assertEqual(len(external), 2)
        candidate = next(
            item for item in mars
            if item["path"].endswith("mars-colony-governor.lisp")
        )
        self.assertEqual(candidate["proof"], "local-executed")
        self.assertEqual(candidate["external_status"], "unverified")

    def test_hosted_examples_are_parseable_and_profile_tagged(self):
        hosted = [
            item
            for item in self.manifest["examples"]
            if item["runtime"] == "python-hosted"
        ]
        self.assertEqual(len(hosted), 2)
        self.assertEqual(
            {item["profile"] for item in hosted},
            {"hosted-governor@2", "mars-governor-candidate@2"},
        )
        for item in hosted:
            self.assertTrue(
                lisp.parse(
                    (ROOT / item["path"]).read_text(encoding="utf-8")
                )
            )
            self.assertEqual(item["proof"], "local-executed")

    def test_manifest_registered_bindings_match_worker_registry(self):
        registered = lisp._registered_sources()
        for item in self.manifest["examples"]:
            source_id = item.get("source_id")
            if source_id is None:
                continue
            with self.subTest(source_id=source_id):
                source = registered[source_id]
                self.assertEqual(item["profile"], source["profile"])
                self.assertEqual(item["contract_id"], source["contract_id"])


if __name__ == "__main__":
    unittest.main()
