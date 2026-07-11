#!/usr/bin/env python3
"""Verify LisPy's pinned userspace compliance with the RAPP spine."""

import argparse
import ast
import hashlib
import json
import re
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "rapp-compliance.json"


def _sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _agent_abi(path):
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    classes = [
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef)
        and any(
            isinstance(base, ast.Name) and base.id == "BasicAgent"
            for base in node.bases
        )
    ]
    if len(classes) != 1:
        return False
    methods = {
        node.name: node
        for node in classes[0].body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    perform = methods.get("perform")
    return bool(
        "__init__" in methods
        and "system_context" in methods
        and perform is not None
        and perform.args.kwarg is not None
        and perform.args.kwarg.arg == "kwargs"
    )


def verify(spine=None):
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    checks = {}
    checks["manifest_schema"] = (
        manifest.get("schema") == "lispy-rapp-compliance/1.0"
    )
    version_match = re.search(
        r'^VERSION = "([^"]+)"$',
        (ROOT / "lisp.py").read_text(encoding="utf-8"),
        re.MULTILINE,
    )
    checks["project_version"] = bool(
        version_match
        and manifest.get("project", {}).get("version")
        == version_match.group(1)
    )
    classification = manifest.get("classification", {})
    checks["userspace_classification"] = (
        classification.get("rapp_role") == "userspace-agent-cubby"
        and classification.get("kernel") is False
        and classification.get("distro") is False
        and classification.get("substrate_runtime") is False
        and classification.get("runtime_parity_claim") == "none"
        and classification.get("network_surface") == []
    )
    for name in ("agent", "cubby"):
        artifact = manifest["artifacts"][name]
        path = ROOT / artifact["path"]
        checks[f"{name}_exists"] = path.is_file()
        checks[f"{name}_sha256"] = (
            path.is_file() and _sha256(path) == artifact["sha256"]
        )
    agent_path = ROOT / manifest["artifacts"]["agent"]["path"]
    checks["agent_filename"] = agent_path.name.endswith("_agent.py")
    checks["agent_abi"] = _agent_abi(agent_path)
    agent_text = agent_path.read_text(encoding="utf-8")
    checks["no_second_wire"] = all(
        marker not in agent_text
        for marker in ("@app.route", "Flask(", "POST /chat")
    )
    cubby = json.loads(
        (ROOT / manifest["artifacts"]["cubby"]["path"]).read_text(
            encoding="utf-8"
        )
    )
    checks["cubby_schema"] = (
        cubby.get("schema") == "rapp-cubby/1.0"
        and cubby.get("slug") == "lisppy"
        and cubby.get("streamable") == {"agents": True}
        and "agents" in cubby.get("estate", {}).get("anatomy", [])
    )
    if spine is not None:
        spine = Path(spine).resolve()
        expected = manifest["spine"]
        checks["spine_commit"] = (
            subprocess.check_output(
                ["git", "-C", str(spine), "rev-parse", "HEAD"],
                text=True,
            ).strip()
            == expected["commit"]
        )
        for relative, digest in expected["artifacts"].items():
            checks[f"spine_{relative}"] = _sha256(spine / relative) == digest
    return {
        "schema": "lispy-rapp-compliance-receipt/1.0",
        "spine_commit": manifest["spine"]["commit"],
        "classification": classification["rapp_role"],
        "checks": checks,
        "ok": all(checks.values()),
        "known_gaps": manifest["known_gaps"],
    }


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Verify LisPy's RAPP userspace adapter and pinned spine.",
        allow_abbrev=False,
    )
    parser.add_argument(
        "--spine",
        help="optional local checkout of the exact pinned rapp-spine commit",
    )
    args = parser.parse_args(argv)
    receipt = verify(args.spine)
    print(json.dumps(receipt, sort_keys=True, separators=(",", ":")))
    return 0 if receipt["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
