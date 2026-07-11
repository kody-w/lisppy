"""Authoritative registered-governor frame composition."""

from effect_executor import (
    _preflight,
    _sha256,
    execute_effects_batch,
    proposal_sha256,
)
from lisp import registered_source, run_registered_governor


def run_hosted_frame(
    source_id,
    *,
    expected_source_sha256,
    inputs,
    mutable_outputs,
    intent_scope,
    registry,
    store,
    namespace,
    execution_id,
    limits=None,
):
    proposal = run_registered_governor(
        source_id,
        expected_source_sha256=expected_source_sha256,
        inputs=inputs,
        mutable_outputs=mutable_outputs,
        intent_scope=intent_scope,
        limits=limits,
    )
    if proposal["status"] != "accepted":
        return {
            "api": "lispy.hosted-frame/v1",
            "status": "rolled_back",
            "source_id": source_id,
            "proposal": proposal,
            "execution": None,
            "committed_outputs": None,
            "reconciliation_required": False,
        }

    adapter_ids = {
        effect_type: registry.get(effect_type)["adapter_id"]
        for effect_type in {
            effect["type"]
            for effect in proposal["effects"]
        }
    }
    expected = {
        "source_id": proposal["source_id"],
        "source_sha256": proposal["source_sha256"],
        "contract_id": proposal["contract_id"],
        "intent_scope": proposal["intent_scope"],
        "proposal_sha256": proposal_sha256(proposal),
        "namespace": namespace,
        "adapter_ids": adapter_ids,
    }
    execution = execute_effects_batch(
        proposal,
        expected=expected,
        registry=registry,
        store=store,
        namespace=namespace,
        execution_id=execution_id,
    )
    committed = (
        execution["status"] == "completed"
        and all(
            effect["status"] in ("applied", "duplicate_applied")
            for effect in execution["effects"]
        )
    )
    return {
        "api": "lispy.hosted-frame/v1",
        "status": "committed" if committed else "reconciliation_required",
        "source_id": source_id,
        "proposal": proposal,
        "execution": execution,
        "committed_outputs": proposal["outputs"] if committed else None,
        "reconciliation_required": not committed,
    }


def run_hosted_frame_v2(
    source_id,
    *,
    expected_source_sha256,
    inputs,
    mutable_outputs,
    intent_scope,
    registry,
    store,
    namespace,
    execution_id,
    limits=None,
):
    try:
        proposal = run_registered_governor(
            source_id,
            expected_source_sha256=expected_source_sha256,
            inputs=inputs,
            mutable_outputs=mutable_outputs,
            intent_scope=intent_scope,
            limits=limits,
        )
    except Exception:
        return _frame_v2_error(
            source_id,
            "rejected",
            "proposal_preflight_failed",
            proposal=None,
            reconciliation_required=False,
        )
    if proposal["status"] != "accepted":
        return _frame_v2_error(
            source_id,
            "rolled_back",
            "proposal_rolled_back",
            proposal=proposal,
            reconciliation_required=False,
        )

    try:
        if not registry.frozen:
            raise ValueError("adapter registry is not frozen")
        adapter_ids = {
            effect_type: registry.get(effect_type)["adapter_id"]
            for effect_type in {
                effect["type"]
                for effect in proposal["effects"]
            }
        }
        if not isinstance(namespace, str) or not namespace:
            raise ValueError("namespace is invalid")
        if not isinstance(execution_id, str) or not execution_id:
            raise ValueError("execution_id is invalid")
        required_store_methods = {
            "abort_batch",
            "abort_reserved_batch",
            "begin_batch_execution",
            "mark_indeterminate",
            "release_batch",
            "reserve_batch",
            "succeed",
        }
        if any(
            not callable(getattr(store, name, None))
            for name in required_store_methods
        ):
            raise ValueError("store does not implement the batch protocol")
    except Exception:
        return _frame_v2_error(
            source_id,
            "rejected",
            "authority_preflight_failed",
            proposal=proposal,
            reconciliation_required=False,
        )

    expected = {
        "source_id": proposal["source_id"],
        "source_sha256": proposal["source_sha256"],
        "contract_id": proposal["contract_id"],
        "intent_scope": proposal["intent_scope"],
        "proposal_sha256": proposal_sha256(proposal),
        "namespace": namespace,
        "adapter_ids": adapter_ids,
    }
    try:
        _preflight(proposal, expected, registry)
    except Exception:
        return _frame_v2_error(
            source_id,
            "rejected",
            "effect_preflight_failed",
            proposal=proposal,
            reconciliation_required=False,
            phase="preflight",
        )
    try:
        execution = execute_effects_batch(
            proposal,
            expected=expected,
            registry=registry,
            store=store,
            namespace=namespace,
            execution_id=execution_id,
        )
    except Exception:
        return _frame_v2_error(
            source_id,
            "reconciliation_required",
            "execution_state_unknown",
            proposal=proposal,
            reconciliation_required=True,
            phase="execute",
        )

    try:
        _validate_frame_execution(execution, proposal, expected, execution_id)
    except Exception:
        return _frame_v2_error(
            source_id,
            "reconciliation_required",
            "execution_receipt_invalid",
            proposal=proposal,
            reconciliation_required=True,
            phase="execute",
        )
    execution_status = execution["status"]
    committed = execution_status == "completed"
    if execution_status == "rejected":
        reconciliation_required = execution["reservation"] == "indeterminate"
        status = (
            "reconciliation_required"
            if reconciliation_required
            else "rejected"
        )
        code = (
            "execution_indeterminate"
            if reconciliation_required
            else "execution_rejected"
        )
    elif committed:
        reconciliation_required = False
        status = "committed"
        code = None
    else:
        reconciliation_required = True
        status = "reconciliation_required"
        code = "execution_indeterminate"
    return {
        "api": "lispy.hosted-frame/v2",
        "status": status,
        "phase": "commit" if committed else "execute",
        "execution_status": execution_status,
        "commit_kind": (
            "decision_only"
            if committed and not proposal["effects"]
            else "effects_applied"
            if committed
            else None
        ),
        "source_id": source_id,
        "proposal": proposal,
        "execution": execution,
        "committed_outputs": proposal["outputs"] if committed else None,
        "reconciliation_required": reconciliation_required,
        "error": (
            None
            if code is None
            else {
                "category": "hosted-frame",
                "code": code,
            }
        ),
    }


def _frame_v2_error(
    source_id,
    status,
    code,
    *,
    proposal,
    reconciliation_required,
    phase="preflight",
):
    return {
        "api": "lispy.hosted-frame/v2",
        "status": status,
        "phase": phase,
        "execution_status": None,
        "commit_kind": None,
        "source_id": source_id,
        "proposal": proposal,
        "execution": None,
        "committed_outputs": None,
        "reconciliation_required": reconciliation_required,
        "error": {
            "category": "hosted-frame",
            "code": code,
        },
    }


def _validate_frame_execution(execution, proposal, expected, execution_id):
    required = {
        "api",
        "execution_id",
        "namespace",
        "proposal_sha256",
        "authority",
        "authority_sha256",
        "reservation",
        "status",
        "effects",
    }
    if not isinstance(execution, dict) or set(execution) != required:
        raise ValueError("invalid execution receipt fields")
    if (
        execution["api"] != "lispy.effects-execution/v2"
        or execution["execution_id"] != execution_id
        or execution["namespace"] != expected["namespace"]
        or execution["proposal_sha256"] != expected["proposal_sha256"]
        or execution["authority"] != {
            key: expected[key]
            for key in (
                "source_id",
                "source_sha256",
                "contract_id",
                "intent_scope",
                "proposal_sha256",
                "namespace",
                "adapter_ids",
            )
        }
        or execution["authority_sha256"] != _sha256(execution["authority"])
        or execution["reservation"]
        not in ("conflict", "indeterminate", "reserved")
        or execution["status"]
        not in ("completed", "partially_applied", "rejected", "stopped")
        or not isinstance(execution["effects"], list)
        or len(execution["effects"]) != len(proposal["effects"])
    ):
        raise ValueError("invalid execution receipt identity")
    for proposed, executed in zip(proposal["effects"], execution["effects"]):
        if (
            not isinstance(executed, dict)
            or set(executed) != {
                "sequence",
                "type",
                "idempotency_key",
                "effect_sha256",
                "adapter_id",
                "status",
                "result",
                "error",
            }
            or executed.get("sequence") != proposed["sequence"]
            or executed.get("type") != proposed["type"]
            or executed.get("idempotency_key")
            != proposed["idempotency_key"]
            or executed.get("effect_sha256") != proposed["effect_sha256"]
            or executed.get("adapter_id")
            != expected["adapter_ids"].get(proposed["type"])
        ):
            raise ValueError("execution effect does not match proposal")
        effect_status = executed["status"]
        if effect_status not in (
            "applied",
            "conflict",
            "duplicate_applied",
            "indeterminate",
            "not_attempted",
            "not_reserved",
        ):
            raise ValueError("invalid execution effect status")
        if effect_status == "applied":
            if executed["error"] is not None:
                raise ValueError("applied execution effect has an error")
        elif effect_status == "duplicate_applied":
            if (
                executed["error"] is not None
                and (
                    executed["result"] is not None
                    or not isinstance(executed["error"], dict)
                )
            ):
                raise ValueError("invalid duplicate execution evidence")
        elif executed["result"] is not None:
            raise ValueError("incomplete execution effect has a result")
    valid_status_reservations = {
        "completed": {"reserved"},
        "partially_applied": {"reserved"},
        "stopped": {"reserved"},
        "rejected": {"conflict", "indeterminate"},
    }
    if execution["reservation"] not in valid_status_reservations[
        execution["status"]
    ]:
        raise ValueError("execution reservation/status mismatch")
    if execution["status"] == "completed" and any(
        item.get("status") not in ("applied", "duplicate_applied")
        for item in execution["effects"]
    ):
        raise ValueError("completed execution contains incomplete effects")
    return execution


__all__ = [
    "registered_source",
    "run_registered_governor",
    "run_hosted_frame",
    "run_hosted_frame_v2",
]
