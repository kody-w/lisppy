"""RAPP userspace adapter for the LisPy runtime.

This file is intentionally a drop-in ``*_agent.py`` cartridge. It adds LisPy as
RAPP userspace without changing the sacred brainstem kernel or adding a route.
"""

import json

try:
    from basic_agent import BasicAgent
except ImportError:
    from agents.basic_agent import BasicAgent


class LispyRuntimeAgent(BasicAgent):
    """Expose safe LisPy evaluation through the frozen RAPP agent ABI."""

    def __init__(self):
        self.name = "LispyRuntime"
        self.metadata = {
            "name": self.name,
            "description": (
                "Evaluate safe LisPy source, inspect the installed Core contract, "
                "or run the deterministic offline hosted-frame demonstration."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": [
                            "evaluate",
                            "contract_manifest",
                            "hosted_demo",
                        ],
                        "description": (
                            "Operation to perform. Use evaluate for LisPy source, "
                            "contract_manifest for the pinned Core contract identity, "
                            "or hosted_demo for the offline two-frame proof."
                        ),
                    },
                    "source": {
                        "type": "string",
                        "description": (
                            "LisPy source for action=evaluate. Evaluation is safe-profile "
                            "only: no filesystem writes, Python execution, credentials, "
                            "or external effect adapters."
                        ),
                    },
                },
                "required": ["action"],
            },
        }
        super().__init__()

    def system_context(self):
        return (
            "LispyRuntime is a safe userspace capability. It proposes and evaluates "
            "locally; it does not create a second RAPP wire or modify the brainstem."
        )

    def perform(self, **kwargs) -> str:
        action = kwargs.get("action")
        try:
            if action == "evaluate":
                source = kwargs.get("source")
                if not isinstance(source, str) or not source.strip():
                    return self._error("source_required")
                from lisppy import LispyVM

                result = LispyVM(profile="core").execute(source)
                payload = {
                    "schema": "lispy-rapp-agent-result/1.0",
                    "action": action,
                    "ok": result.ok,
                    "result": result.as_wire_dict(),
                    "error": None,
                }
            elif action == "contract_manifest":
                from lisppy import contract_manifest

                payload = {
                    "schema": "lispy-rapp-agent-result/1.0",
                    "action": action,
                    "ok": True,
                    "result": contract_manifest(),
                    "error": None,
                }
            elif action == "hosted_demo":
                from lisppy import run_demo

                payload = {
                    "schema": "lispy-rapp-agent-result/1.0",
                    "action": action,
                    "ok": True,
                    "result": run_demo(),
                    "error": None,
                }
            else:
                return self._error("unsupported_action")
        except ModuleNotFoundError:
            return self._error("lisppy_dependency_missing")
        except Exception:
            return self._error("operation_failed")
        return json.dumps(
            payload,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        )

    @staticmethod
    def _error(code):
        return json.dumps(
            {
                "schema": "lispy-rapp-agent-result/1.0",
                "action": None,
                "ok": False,
                "result": None,
                "error": {
                    "category": "lispy-rapp-agent",
                    "code": code,
                },
            },
            sort_keys=True,
            separators=(",", ":"),
        )
