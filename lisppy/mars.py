"""Local, deterministic evidence for the registered Mars governor candidate."""

import argparse
import hashlib
import json
import pkgutil

from lisp import registered_source, run_registered_governor


SOURCE_ID = "mars-barn/governor-example"
CONTRACT_RESOURCE = "data/mars/governor-contract.json"
VECTORS_RESOURCE = "data/mars/governor-vectors.json"


def _resource(path):
    content = pkgutil.get_data("lisppy", path)
    if content is None:
        raise RuntimeError(f"missing Mars evidence resource: {path}")
    return content


def load_mars_contract():
    value = json.loads(_resource(CONTRACT_RESOURCE).decode("utf-8"))
    required = {
        "schema",
        "source_id",
        "profile",
        "contract_id",
        "source_sha256",
        "execution_kind",
        "external_target_status",
        "required_inputs",
        "mutable_outputs",
        "invariants",
        "priority",
        "thresholds",
        "effect_types",
    }
    if (
        not isinstance(value, dict)
        or set(value) != required
        or value["schema"] != "lispy.mars-governor-contract/v1"
        or value["source_id"] != SOURCE_ID
        or value["execution_kind"] != "decision_only"
        or value["effect_types"] != []
    ):
        raise ValueError("invalid Mars governor contract")
    return value


def load_mars_vectors():
    value = json.loads(_resource(VECTORS_RESOURCE).decode("utf-8"))
    if (
        not isinstance(value, dict)
        or set(value)
        != {
            "schema",
            "contract_id",
            "source_sha256",
            "initial_outputs",
            "vectors",
        }
        or value["schema"] != "lispy.mars-governor-vectors/v1"
        or not isinstance(value["vectors"], list)
        or not value["vectors"]
    ):
        raise ValueError("invalid Mars governor vectors")
    return value


def run_mars_vectors():
    contract_bytes = _resource(CONTRACT_RESOURCE)
    vector_bytes = _resource(VECTORS_RESOURCE)
    contract = load_mars_contract()
    vectors = load_mars_vectors()
    source = registered_source(SOURCE_ID)
    if (
        contract["source_sha256"] != source["source_sha256"]
        or vectors["source_sha256"] != source["source_sha256"]
        or vectors["contract_id"] != contract["contract_id"]
    ):
        raise ValueError("Mars source or contract identity drift")
    results = []
    for vector in vectors["vectors"]:
        receipt = run_registered_governor(
            SOURCE_ID,
            expected_source_sha256=source["source_sha256"],
            inputs=vector["inputs"],
            mutable_outputs=vectors["initial_outputs"],
            intent_scope=f"mars-vector:{vector['id']}",
        )
        normalized_logs = (
            receipt["logs"].replace("₂", "2").replace("⚠️", "")
        )
        checks = {
            "accepted": receipt["status"] == "accepted",
            "outputs": receipt["outputs"] == vector["outputs"],
            "writes": receipt["writes"]
            == [
                "food_ration",
                "greenhouse_alloc",
                "heating_alloc",
                "isru_alloc",
            ],
            "no_effects": receipt["effects"] == [],
            "log": vector["log_contains"] in normalized_logs,
        }
        results.append(
            {
                "id": vector["id"],
                "ok": all(checks.values()),
                "checks": checks,
                "outputs": receipt["outputs"],
            }
        )
    payload = {
        "api": "lispy.mars-policy-evidence/v1",
        "source_id": SOURCE_ID,
        "source_sha256": source["source_sha256"],
        "contract_sha256": hashlib.sha256(contract_bytes).hexdigest(),
        "vectors_sha256": hashlib.sha256(vector_bytes).hexdigest(),
        "external_target_status": contract["external_target_status"],
        "results": results,
        "summary": {
            "total": len(results),
            "passed": sum(result["ok"] for result in results),
        },
    }
    payload["ok"] = payload["summary"]["passed"] == payload["summary"]["total"]
    return payload


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="python -m lisppy.mars",
        description="Run local Mars governor policy vectors.",
        allow_abbrev=False,
    )
    parser.add_argument("--pretty", action="store_true")
    args = parser.parse_args(argv)
    try:
        report = run_mars_vectors()
    except Exception:
        report = {
            "api": "lispy.error/v1",
            "ok": False,
            "error": {
                "category": "mars-evidence",
                "code": "verification_failed",
            },
        }
    print(
        json.dumps(
            report,
            ensure_ascii=True,
            sort_keys=True,
            separators=None if args.pretty else (",", ":"),
            indent=2 if args.pretty else None,
        )
    )
    return 0 if report.get("ok") is True else 1


if __name__ == "__main__":
    raise SystemExit(main())
