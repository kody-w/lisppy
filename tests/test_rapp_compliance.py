import importlib.util
import json
import sys
import types
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
AGENT_PATH = ROOT / "agents" / "lispy_runtime_agent.py"
VERIFY_PATH = ROOT / "tools" / "verify_rapp_compliance.py"


def load_agent_module():
    basic_agent = types.ModuleType("basic_agent")

    class BasicAgent:
        def __init__(self):
            pass

        def to_tool(self):
            return {
                "type": "function",
                "function": self.metadata,
            }

    basic_agent.BasicAgent = BasicAgent
    spec = importlib.util.spec_from_file_location(
        "lispy_runtime_agent_test",
        AGENT_PATH,
    )
    module = importlib.util.module_from_spec(spec)
    with mock.patch.dict(sys.modules, {"basic_agent": basic_agent}):
        spec.loader.exec_module(module)
    return module


class RappComplianceTests(unittest.TestCase):
    def test_cubby_manifest_is_strict_and_streams_only_agents(self):
        cubby = json.loads(
            (ROOT / "cubby.json").read_text(encoding="utf-8")
        )
        required = {
            "schema",
            "github_login",
            "slug",
            "display_name",
            "what_im_cooking",
            "created_at",
            "estate",
            "streamable",
        }
        self.assertEqual(set(cubby), required)
        self.assertEqual(cubby["schema"], "rapp-cubby/1.0")
        self.assertEqual(cubby["slug"], "lisppy")
        self.assertEqual(cubby["estate"]["anatomy"], ["agents"])
        self.assertEqual(cubby["streamable"], {"agents": True})

    def test_drop_in_agent_honors_the_frozen_basic_agent_abi(self):
        module = load_agent_module()
        agent = module.LispyRuntimeAgent()
        self.assertEqual(agent.name, "LispyRuntime")
        self.assertEqual(agent.metadata["name"], agent.name)
        self.assertEqual(
            set(agent.metadata),
            {"name", "description", "parameters"},
        )
        self.assertIn("action", agent.metadata["parameters"]["properties"])
        self.assertIsInstance(agent.system_context(), str)
        tool = agent.to_tool()
        self.assertEqual(tool["function"], agent.metadata)

        evaluated = json.loads(
            agent.perform(
                action="evaluate",
                source="(+ 2 3)",
                user_guid="kernel-may-inject-extra-kwargs",
            )
        )
        self.assertTrue(evaluated["ok"])
        self.assertEqual(
            evaluated["result"]["value"],
            {"tag": "integer", "value": "5"},
        )
        manifest = json.loads(
            agent.perform(action="contract_manifest", ignored=True)
        )
        self.assertTrue(manifest["ok"])
        self.assertEqual(
            manifest["result"]["api"],
            "lispy.contract-manifest/v1",
        )
        failure = json.loads(agent.perform(action="evaluate"))
        self.assertFalse(failure["ok"])
        self.assertEqual(failure["error"]["code"], "source_required")
        self.assertIsInstance(agent.perform(action="unknown"), str)

    def test_agent_exposes_no_second_wire_or_trusted_profile(self):
        source = AGENT_PATH.read_text(encoding="utf-8")
        for forbidden in (
            "@app.route",
            "Flask(",
            "POST /chat",
            "trusted=True",
            "rb-run",
        ):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, source)

    def test_machine_compliance_receipt_is_complete(self):
        spec = importlib.util.spec_from_file_location(
            "verify_rapp_compliance_test",
            VERIFY_PATH,
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        receipt = module.verify()
        self.assertTrue(receipt["ok"])
        self.assertEqual(
            receipt["schema"],
            "lispy-rapp-compliance-receipt/1.0",
        )
        self.assertEqual(
            receipt["classification"],
            "userspace-agent-cubby",
        )
        self.assertTrue(all(receipt["checks"].values()))

    def test_compliance_manifest_denies_runtime_parity_claim(self):
        manifest = json.loads(
            (ROOT / "rapp-compliance.json").read_text(encoding="utf-8")
        )
        classification = manifest["classification"]
        self.assertFalse(classification["kernel"])
        self.assertFalse(classification["distro"])
        self.assertFalse(classification["substrate_runtime"])
        self.assertEqual(classification["runtime_parity_claim"], "none")
        self.assertEqual(classification["network_surface"], [])
        parity = next(
            item
            for item in manifest["not_applicable"]
            if item["spec"] == "rapp-runtime-parity/1.0"
        )
        self.assertIn("does not serve POST /chat", parity["reason"])


if __name__ == "__main__":
    unittest.main()
