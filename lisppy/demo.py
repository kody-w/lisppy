"""Deterministic offline hosted-governor and effect-application demonstration."""

import argparse
import json

from lisp import VERSION
from lisppy.effects import (
    EffectAdapterRegistry,
    InMemoryIdempotencyStore,
)
from lisppy.host import registered_source, run_hosted_frame_v2


SOURCE_ID = "lispy/hosted-doctor@1"


def run_demo():
    source = registered_source(SOURCE_ID)
    initial_outputs = {
        "heating_alloc": 0.25,
        "isru_alloc": 0.40,
        "greenhouse_alloc": 0.35,
        "food_ration": 1.0,
    }
    registry = EffectAdapterRegistry()
    calls = []

    def validate(payload):
        return [] if set(payload) == {"channel", "title", "body"} else ["shape"]

    def execute(payload, context):
        calls.append({"payload": payload, "context": context})
        return {"number": 100 + len(calls)}

    registry.register(
        "rappterbook.post.create",
        adapter_id="offline-recording@1",
        validate=validate,
        execute=execute,
    )
    registry.freeze()
    store = InMemoryIdempotencyStore()
    first = run_hosted_frame_v2(
        SOURCE_ID,
        expected_source_sha256=source["source_sha256"],
        inputs={"sol": 1},
        mutable_outputs=initial_outputs,
        intent_scope="offline-frame-1",
        registry=registry,
        store=store,
        namespace="offline-demo",
        execution_id="offline-apply-1",
    )
    replayed = run_hosted_frame_v2(
        SOURCE_ID,
        expected_source_sha256=source["source_sha256"],
        inputs={"sol": 1},
        mutable_outputs=initial_outputs,
        intent_scope="offline-frame-1",
        registry=registry,
        store=store,
        namespace="offline-demo",
        execution_id="offline-apply-2",
    )
    calls_after_replay = len(calls)
    second = run_hosted_frame_v2(
        SOURCE_ID,
        expected_source_sha256=source["source_sha256"],
        inputs={"sol": 2},
        mutable_outputs=first["committed_outputs"],
        intent_scope="offline-frame-2",
        registry=registry,
        store=store,
        namespace="offline-demo",
        execution_id="offline-apply-3",
    )
    report = {
        "api": "lispy.hosted-flow/v2",
        "first_frame": {
            "status": first["status"],
            "outputs": first["committed_outputs"],
            "effect_status": first["execution"]["effects"][0]["status"],
        },
        "idempotent_replay": {
            "effect_status": replayed["execution"]["effects"][0]["status"],
            "adapter_calls": calls_after_replay,
        },
        "second_frame": {
            "status": second["status"],
            "initial_outputs_from_first_frame": first["committed_outputs"],
            "outputs": second["committed_outputs"],
            "adapter_calls_total": len(calls),
        },
        "source_sha256": source["source_sha256"],
    }
    checks = {
        "first_frame_committed": report["first_frame"]["status"] == "committed",
        "first_effect_applied": (
            report["first_frame"]["effect_status"] == "applied"
        ),
        "idempotent_replay": (
            report["idempotent_replay"]["effect_status"] == "duplicate_applied"
            and report["idempotent_replay"]["adapter_calls"] == 1
        ),
        "second_frame_committed": (
            report["second_frame"]["status"] == "committed"
        ),
        "output_handoff": (
            report["second_frame"]["initial_outputs_from_first_frame"]
            == report["first_frame"]["outputs"]
        ),
    }
    report["checks"] = checks
    report["ok"] = all(checks.values())
    return report


def _argument_parser():
    parser = argparse.ArgumentParser(
        prog="python -m lisppy.demo",
        description="Run the deterministic offline hosted-frame demonstration.",
        allow_abbrev=False,
    )
    parser.add_argument(
        "--pretty",
        action="store_true",
        help="indent the JSON report",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"LisPy demo {VERSION}",
    )
    return parser


def main(argv=None):
    args = _argument_parser().parse_args(argv)
    try:
        report = run_demo()
    except Exception:
        report = {
            "api": "lispy.error/v1",
            "ok": False,
            "error": {
                "category": "demo",
                "code": "demo_failed",
                "message": "offline hosted demo failed",
            },
        }
        status = 1
    else:
        status = 0 if report.get("ok") is True else 1
    print(
        json.dumps(
            report,
            ensure_ascii=True,
            sort_keys=True,
            separators=None if args.pretty else (",", ":"),
            indent=2 if args.pretty else None,
        )
    )
    return status


if __name__ == "__main__":
    raise SystemExit(main())
