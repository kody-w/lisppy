import unittest
import copy
from unittest import mock

import lisp
import lisppy.host as host_module
from lisppy.effects import (
    EffectAdapterRegistry,
    InMemoryIdempotencyStore,
    execute_effects_batch,
    proposal_sha256,
)
from lisppy.host import (
    registered_source,
    run_hosted_frame,
    run_hosted_frame_v2,
    run_registered_governor,
)


SOURCE_ID = "lispy/hosted-doctor@1"


def registry(calls, *, fail=False):
    adapters = EffectAdapterRegistry()

    def execute(payload, context):
        calls.append({"payload": payload, "context": context})
        if fail:
            raise RuntimeError("private adapter failure")
        return {"number": len(calls)}

    adapters.register(
        "rappterbook.post.create",
        adapter_id="test-recording@1",
        validate=lambda payload: (
            [] if set(payload) == {"channel", "title", "body"} else ["shape"]
        ),
        execute=execute,
    )
    return adapters.freeze()


class RegisteredHostedFrameTests(unittest.TestCase):
    def options(self, adapters, store, execution_id):
        source = registered_source(SOURCE_ID)
        return {
            "expected_source_sha256": source["source_sha256"],
            "inputs": {"sol": 1},
            "mutable_outputs": {
                "heating_alloc": 0.25,
                "isru_alloc": 0.40,
                "greenhouse_alloc": 0.35,
                "food_ration": 1.0,
            },
            "intent_scope": "test-frame-1",
            "registry": adapters,
            "store": store,
            "namespace": "test-host",
            "execution_id": execution_id,
        }

    def test_registered_frame_commits_and_replays_atomically(self):
        calls = []
        adapters = registry(calls)
        store = InMemoryIdempotencyStore()
        first = run_hosted_frame(
            SOURCE_ID,
            **self.options(adapters, store, "first"),
        )
        second = run_hosted_frame(
            SOURCE_ID,
            **self.options(adapters, store, "second"),
        )
        self.assertEqual(first["status"], "committed")
        self.assertEqual(first["execution"]["api"], "lispy.effects-execution/v2")
        self.assertEqual(first["execution"]["effects"][0]["status"], "applied")
        self.assertEqual(
            second["execution"]["effects"][0]["status"],
            "duplicate_applied",
        )
        self.assertEqual(len(calls), 1)

    def test_adapter_failure_never_commits_candidate_outputs(self):
        calls = []
        adapters = registry(calls, fail=True)
        frame = run_hosted_frame(
            SOURCE_ID,
            **self.options(
                adapters,
                InMemoryIdempotencyStore(),
                "failed",
            ),
        )
        self.assertEqual(frame["status"], "reconciliation_required")
        self.assertIsNone(frame["committed_outputs"])
        self.assertTrue(frame["reconciliation_required"])
        self.assertNotIn("private", str(frame))

    def test_registered_source_pin_and_required_inputs_fail_closed(self):
        source = registered_source(SOURCE_ID)
        with self.assertRaisesRegex(Exception, "SHA-256 mismatch"):
            run_registered_governor(
                SOURCE_ID,
                expected_source_sha256="0" * 64,
                inputs={"sol": 1},
                mutable_outputs={
                    "heating_alloc": 0.25,
                    "isru_alloc": 0.40,
                    "greenhouse_alloc": 0.35,
                    "food_ration": 1.0,
                },
            )

    def test_frame_v2_returns_structured_preflight_rejection(self):
        source = registered_source(SOURCE_ID)
        empty = EffectAdapterRegistry().freeze()
        frame = run_hosted_frame_v2(
            SOURCE_ID,
            expected_source_sha256=source["source_sha256"],
            inputs={"sol": 1},
            mutable_outputs={
                "heating_alloc": 0.25,
                "isru_alloc": 0.40,
                "greenhouse_alloc": 0.35,
                "food_ration": 1.0,
            },
            intent_scope="rejected",
            registry=empty,
            store=InMemoryIdempotencyStore(),
            namespace="test-host",
            execution_id="rejected",
        )
        self.assertEqual(frame["api"], "lispy.hosted-frame/v2")
        self.assertEqual(frame["status"], "rejected")
        self.assertEqual(
            frame["error"]["code"],
            "authority_preflight_failed",
        )
        self.assertIsNone(frame["committed_outputs"])

    def test_frame_v2_marks_post_reservation_failures_uncertain(self):
        class MalformedReservationStore(InMemoryIdempotencyStore):
            def reserve_batch(self, namespace, reservations, owner):
                response = super().reserve_batch(
                    namespace,
                    reservations,
                    owner,
                )
                self.reserved = True
                return {
                    "decision": "reserved",
                    "batch_token": response["batch_token"],
                    "claims": [],
                }

        calls = []
        adapters = registry(calls)
        store = MalformedReservationStore()
        frame = run_hosted_frame_v2(
            SOURCE_ID,
            **self.options(adapters, store, "uncertain"),
        )
        self.assertTrue(store.reserved)
        self.assertEqual(frame["status"], "reconciliation_required")
        self.assertTrue(frame["reconciliation_required"])
        self.assertEqual(frame["error"]["code"], "execution_state_unknown")
        self.assertIsNotNone(frame["proposal"])
        self.assertEqual(calls, [])
        self.assertEqual(store._records, {})

    def test_frame_v2_effect_preflight_never_calls_store(self):
        class CountingStore(InMemoryIdempotencyStore):
            reserve_calls = 0

            def reserve_batch(self, namespace, reservations, owner):
                self.reserve_calls += 1
                return super().reserve_batch(namespace, reservations, owner)

        adapters = EffectAdapterRegistry()
        adapters.register(
            "rappterbook.post.create",
            adapter_id="rejecting@1",
            validate=lambda _payload: ["rejected"],
            execute=lambda _payload, _context: None,
        )
        adapters.freeze()
        store = CountingStore()
        frame = run_hosted_frame_v2(
            SOURCE_ID,
            **self.options(adapters, store, "preflight"),
        )
        self.assertEqual(frame["status"], "rejected")
        self.assertEqual(frame["error"]["code"], "effect_preflight_failed")
        self.assertFalse(frame["reconciliation_required"])
        self.assertEqual(store.reserve_calls, 0)

    def test_frame_v2_rejects_malformed_execution_receipt_as_uncertain(self):
        calls = []
        adapters = registry(calls)
        with mock.patch.object(
            host_module,
            "execute_effects_batch",
            return_value={
                "api": "lispy.effects-execution/v2",
                "execution_id": "malformed",
                "namespace": "test-host",
                "proposal_sha256": "0" * 64,
                "authority": {},
                "authority_sha256": "0" * 64,
                "reservation": "reserved",
                "status": "completed",
                "effects": [],
            },
        ):
            frame = run_hosted_frame_v2(
                SOURCE_ID,
                **self.options(
                    adapters,
                    InMemoryIdempotencyStore(),
                    "malformed",
                ),
            )
        self.assertEqual(frame["status"], "reconciliation_required")
        self.assertEqual(frame["error"]["code"], "execution_receipt_invalid")
        self.assertIsNone(frame["committed_outputs"])

    def test_frame_v2_missing_store_is_safe_authority_rejection(self):
        calls = []
        adapters = registry(calls)
        frame = run_hosted_frame_v2(
            SOURCE_ID,
            **self.options(adapters, None, "missing-store"),
        )
        self.assertEqual(frame["status"], "rejected")
        self.assertEqual(
            frame["error"]["code"],
            "authority_preflight_failed",
        )
        self.assertFalse(frame["reconciliation_required"])

    def test_frame_v2_surfaces_proposal_rollback_phase(self):
        rolled_back = {
            "api": "lispy.hosted-governor/v2",
            "status": "rolled_back",
            "error": {"phase": "evaluate"},
        }
        with mock.patch.object(
            host_module,
            "run_registered_governor",
            return_value=rolled_back,
        ):
            frame = run_hosted_frame_v2(
                SOURCE_ID,
                expected_source_sha256="0" * 64,
                inputs={},
                mutable_outputs={},
                intent_scope="rollback",
                registry=EffectAdapterRegistry().freeze(),
                store=InMemoryIdempotencyStore(),
                namespace="test",
                execution_id="rollback",
            )
        self.assertEqual(frame["status"], "rolled_back")
        self.assertEqual(frame["error"]["code"], "proposal_rolled_back")
        self.assertFalse(frame["reconciliation_required"])

    def test_direct_and_worker_proposals_share_semantic_identity(self):
        calls = []
        adapters = registry(calls)
        store = InMemoryIdempotencyStore()
        direct = run_hosted_frame(
            SOURCE_ID,
            **self.options(adapters, store, "direct"),
        )
        source = registered_source(SOURCE_ID)
        request = {
            "api": lisp.WORKER_API,
            "id": "worker-equivalent",
            "op": "hosted-governor",
            "source_id": SOURCE_ID,
            "expected_source_sha256": source["source_sha256"],
            "inputs": {"sol": 1},
            "mutable_outputs": {
                "heating_alloc": 0.25,
                "isru_alloc": 0.40,
                "greenhouse_alloc": 0.35,
                "food_ration": 1.0,
            },
            "contract_id": source["contract_id"],
            "intent_scope": "test-frame-1",
        }
        response = lisp._handle_worker_request(request)
        self.assertTrue(response["ok"])
        worker = response["receipt"]
        self.assertEqual(
            proposal_sha256(direct["proposal"]),
            proposal_sha256(worker),
        )
        replay = execute_effects_batch(
            worker,
            expected={
                "source_id": worker["source_id"],
                "source_sha256": worker["source_sha256"],
                "contract_id": worker["contract_id"],
                "intent_scope": worker["intent_scope"],
                "proposal_sha256": proposal_sha256(worker),
                "namespace": "test-host",
                "adapter_ids": {
                    "rappterbook.post.create": "test-recording@1",
                },
            },
            registry=adapters,
            store=store,
            namespace="test-host",
            execution_id="worker",
        )
        self.assertEqual(
            replay["effects"][0]["status"],
            "duplicate_applied",
        )
        self.assertEqual(len(calls), 1)
        transported = dict(worker)
        transported["transport_note"] = "ignored"
        self.assertEqual(
            proposal_sha256(transported),
            proposal_sha256(worker),
        )
        with self.assertRaisesRegex(Exception, "missing inputs"):
            run_registered_governor(
                SOURCE_ID,
                expected_source_sha256=source["source_sha256"],
                inputs={},
                mutable_outputs={
                    "heating_alloc": 0.25,
                    "isru_alloc": 0.40,
                    "greenhouse_alloc": 0.35,
                    "food_ration": 1.0,
                },
            )

    def test_execution_receipt_validator_rejects_contradictions(self):
        calls = []
        frame = run_hosted_frame(
            SOURCE_ID,
            **self.options(
                registry(calls),
                InMemoryIdempotencyStore(),
                "valid-receipt",
            ),
        )
        execution = frame["execution"]
        proposal = frame["proposal"]
        expected = execution["authority"]
        mutations = []
        reservation = copy.deepcopy(execution)
        reservation["reservation"] = "conflict"
        mutations.append(reservation)
        adapter = copy.deepcopy(execution)
        adapter["effects"][0]["adapter_id"] = "wrong@1"
        mutations.append(adapter)
        empty = copy.deepcopy(execution)
        empty["effects"] = []
        mutations.append(empty)
        for value in mutations:
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    host_module._validate_frame_execution(
                        value,
                        proposal,
                        expected,
                        "valid-receipt",
                    )

    def test_mars_zero_effect_frame_is_explicitly_decision_only(self):
        source_id = "mars-barn/governor-example"
        source = registered_source(source_id)
        frame = run_hosted_frame_v2(
            source_id,
            expected_source_sha256=source["source_sha256"],
            inputs={
                "sol": 1,
                "o2_days": 20.0,
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
            intent_scope="mars-decision-only",
            registry=EffectAdapterRegistry().freeze(),
            store=InMemoryIdempotencyStore(),
            namespace="mars-local",
            execution_id="mars-decision",
        )
        self.assertEqual(frame["status"], "committed")
        self.assertEqual(frame["commit_kind"], "decision_only")
        self.assertEqual(frame["execution"]["effects"], [])


if __name__ == "__main__":
    unittest.main()
