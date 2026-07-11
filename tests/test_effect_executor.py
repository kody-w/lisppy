import copy
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import effect_executor
import lisp


SOURCE = """
(begin
  (rb-post "code" "Title" "Body")
  (set! heating_alloc 0.5))
"""

SOURCE_TWO = """
(begin
  (rb-post "code" "First" "Body")
  (rb-post "code" "Second" "Body")
  (set! heating_alloc 0.5))
"""


def proposal(source=SOURCE, scope="scope-1"):
    return lisp.run_hosted_governor(
        source,
        inputs={"sol": 1},
        mutable_outputs={"heating_alloc": 0.25},
        contract_id="test/effects@1",
        intent_scope=scope,
        source_id="test/source@1",
    )


def expected(receipt):
    return {
        "source_id": receipt["source_id"],
        "source_sha256": receipt["source_sha256"],
        "contract_id": receipt["contract_id"],
        "intent_scope": receipt["intent_scope"],
        "proposal_sha256": effect_executor.proposal_sha256(receipt),
    }


def expected_batch(receipt, namespace="test"):
    return {
        **expected(receipt),
        "namespace": namespace,
        "adapter_ids": {
            "rappterbook.post.create": "recording@1",
        },
    }


def registry(calls, fail=False):
    value = effect_executor.EffectAdapterRegistry()

    def validate(payload):
        return [] if set(payload) == {"channel", "title", "body"} else ["shape"]

    def execute(payload, context):
        calls.append((payload, context))
        if fail:
            raise RuntimeError("secret transport failure")
        return {"number": 101}

    value.register(
        "rappterbook.post.create",
        adapter_id="recording@1",
        validate=validate,
        execute=execute,
    )
    return value.freeze()


class EffectExecutorTests(unittest.TestCase):
    def test_executes_once_then_reports_duplicate(self):
        receipt = proposal()
        calls = []
        adapters = registry(calls)
        store = effect_executor.InMemoryIdempotencyStore()
        options = {
            "expected": expected(receipt),
            "registry": adapters,
            "store": store,
            "namespace": "test",
            "execution_id": "execution-1",
        }
        first = effect_executor.execute_effects(receipt, **options)
        second = effect_executor.execute_effects(receipt, **options)
        self.assertEqual(first["status"], "completed")
        self.assertEqual(first["effects"][0]["status"], "applied")
        self.assertEqual(second["effects"][0]["status"], "duplicate_applied")
        self.assertEqual(len(calls), 1)
        first["effects"][0]["result"]["number"] = 999
        third = effect_executor.execute_effects(receipt, **options)
        self.assertEqual(third["effects"][0]["result"]["number"], 101)

    def test_requires_independent_proposal_pin_and_frozen_registry(self):
        receipt = proposal()
        calls = []
        adapters = registry(calls)
        pins = expected(receipt)
        pins["proposal_sha256"] = "0" * 64
        with self.assertRaises(effect_executor.EffectExecutionError):
            effect_executor.execute_effects(
                receipt,
                expected=pins,
                registry=adapters,
                store=effect_executor.InMemoryIdempotencyStore(),
                namespace="test",
                execution_id="execution-1",
            )
        with self.assertRaises(TypeError):
            adapters.get("rappterbook.post.create")["adapter_id"] = "mutated"
        self.assertEqual(calls, [])

    def test_unknown_store_decision_fails_closed(self):
        class InvalidStore:
            def claim(self, *_args):
                return {"decision": "surprise"}

        receipt = proposal()
        calls = []
        result = effect_executor.execute_effects(
            receipt,
            expected=expected(receipt),
            registry=registry(calls),
            store=InvalidStore(),
            namespace="test",
            execution_id="execution-1",
        )
        self.assertEqual(result["status"], "stopped")
        self.assertEqual(result["effects"][0]["status"], "indeterminate")
        self.assertEqual(calls, [])

    def test_claim_without_token_fails_closed(self):
        class MissingTokenStore:
            def claim(self, *_args):
                return {"decision": "claimed"}

        receipt = proposal()
        calls = []
        result = effect_executor.execute_effects(
            receipt,
            expected=expected(receipt),
            registry=registry(calls),
            store=MissingTokenStore(),
            namespace="test",
            execution_id="execution-1",
        )
        self.assertEqual(result["namespace"], "test")
        self.assertEqual(result["effects"][0]["status"], "indeterminate")
        self.assertEqual(calls, [])

    def test_changed_payload_with_same_key_is_conflict(self):
        receipt = proposal()
        calls = []
        adapters = registry(calls)
        store = effect_executor.InMemoryIdempotencyStore()
        options = {
            "expected": expected(receipt),
            "registry": adapters,
            "store": store,
            "namespace": "test",
            "execution_id": "execution-1",
        }
        effect_executor.execute_effects(receipt, **options)

        changed = copy.deepcopy(receipt)
        effect = changed["effects"][0]
        effect["payload"]["title"] = "Changed"
        effect["effect_sha256"] = lisp.effect_digest(
            changed["source_sha256"],
            effect["type"],
            effect["payload"],
        )
        conflict = effect_executor.execute_effects(
            changed,
            **{**options, "expected": expected(changed)},
        )
        self.assertEqual(conflict["status"], "rejected")
        self.assertEqual(conflict["effects"][0]["status"], "conflict")
        self.assertEqual(len(calls), 1)

    def test_rejects_rolled_back_proposal_before_adapter(self):
        receipt = lisp.run_hosted_governor(
            '(begin (rb-post "code" "Title" "Body") (error "boom"))',
            inputs={"sol": 1},
            mutable_outputs={"heating_alloc": 0.25},
            contract_id="test/effects@1",
            intent_scope="scope-1",
            source_id="test/source@1",
        )
        calls = []
        with self.assertRaises(effect_executor.EffectExecutionError):
            effect_executor.execute_effects(
                receipt,
                expected=expected(receipt),
                registry=registry(calls),
                store=effect_executor.InMemoryIdempotencyStore(),
                namespace="test",
                execution_id="execution-1",
            )
        self.assertEqual(calls, [])

    def test_adapter_error_is_indeterminate_without_secret_leak(self):
        receipt = proposal()
        calls = []
        result = effect_executor.execute_effects(
            receipt,
            expected=expected(receipt),
            registry=registry(calls, fail=True),
            store=effect_executor.InMemoryIdempotencyStore(),
            namespace="test",
            execution_id="execution-1",
        )
        self.assertEqual(result["status"], "stopped")
        self.assertEqual(result["effects"][0]["status"], "indeterminate")
        self.assertNotIn("secret", str(result))

    def test_custom_adapter_exception_is_redacted_and_indeterminate(self):
        class TransportFailure(Exception):
            pass

        receipt = proposal()
        adapters = effect_executor.EffectAdapterRegistry()
        adapters.register(
            "rappterbook.post.create",
            adapter_id="recording@1",
            validate=lambda _payload: [],
            execute=lambda _payload, _context: (_ for _ in ()).throw(
                TransportFailure("private transport detail")
            ),
        )
        adapters.freeze()
        store = effect_executor.InMemoryIdempotencyStore()
        result = effect_executor.execute_effects(
            receipt,
            expected=expected(receipt),
            registry=adapters,
            store=store,
            namespace="test",
            execution_id="custom-error",
        )
        self.assertEqual(result["effects"][0]["status"], "indeterminate")
        self.assertNotIn("private", str(result))
        self.assertEqual(
            [record["state"] for record in store._records.values()],
            ["indeterminate"],
        )

    def test_sqlite_store_survives_reopen(self):
        receipt = proposal()
        calls = []
        adapters = registry(calls)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "effects.sqlite3"
            first_store = effect_executor.SQLiteIdempotencyStore(path)
            effect_executor.execute_effects(
                receipt,
                expected=expected(receipt),
                registry=adapters,
                store=first_store,
                namespace="test",
                execution_id="execution-1",
            )
            first_store.close()

            second_store = effect_executor.SQLiteIdempotencyStore(path)
            replay = effect_executor.execute_effects(
                receipt,
                expected=expected(receipt),
                registry=adapters,
                store=second_store,
                namespace="test",
                execution_id="execution-2",
            )
            second_store.close()
        self.assertEqual(replay["effects"][0]["status"], "duplicate_applied")
        self.assertEqual(len(calls), 1)

    def test_batch_applies_then_replays_without_duplicate_adapter_calls(self):
        receipt = proposal(SOURCE_TWO)
        calls = []
        adapters = registry(calls)
        store = effect_executor.InMemoryIdempotencyStore()
        options = {
            "expected": expected_batch(receipt),
            "registry": adapters,
            "store": store,
            "namespace": "test",
            "execution_id": "batch-1",
        }
        first = effect_executor.execute_effects_batch(receipt, **options)
        second = effect_executor.execute_effects_batch(
            receipt,
            **{**options, "execution_id": "batch-2"},
        )
        self.assertEqual(first["status"], "completed")
        self.assertEqual(
            [item["status"] for item in first["effects"]],
            ["applied", "applied"],
        )
        self.assertEqual(
            [item["status"] for item in second["effects"]],
            ["duplicate_applied", "duplicate_applied"],
        )
        self.assertEqual(len(calls), 2)

    def test_batch_custom_adapter_exception_atomically_cleans_tail(self):
        class TransportFailure(Exception):
            pass

        receipt = proposal(SOURCE_TWO)
        calls = []
        adapters = effect_executor.EffectAdapterRegistry()
        adapters.register(
            "rappterbook.post.create",
            adapter_id="recording@1",
            validate=lambda _payload: [],
            execute=lambda _payload, _context: (
                calls.append("called"),
                (_ for _ in ()).throw(TransportFailure("private")),
            )[1],
        )
        adapters.freeze()
        store = effect_executor.InMemoryIdempotencyStore()
        result = effect_executor.execute_effects_batch(
            receipt,
            expected=expected_batch(receipt),
            registry=adapters,
            store=store,
            namespace="test",
            execution_id="batch-failure",
        )
        self.assertEqual(result["status"], "stopped")
        self.assertEqual(result["effects"][0]["status"], "indeterminate")
        self.assertEqual(result["effects"][1]["status"], "not_attempted")
        self.assertNotIn("private", str(result))
        states = [record["state"] for record in store._records.values()]
        self.assertEqual(states, ["indeterminate"])
        self.assertEqual(calls, ["called"])

    def test_batch_state_is_executing_during_adapter_call(self):
        receipt = proposal()
        with tempfile.TemporaryDirectory() as directory:
            store = effect_executor.SQLiteIdempotencyStore(
                Path(directory) / "effects.sqlite3"
            )
            observed = []
            adapters = effect_executor.EffectAdapterRegistry()

            def execute(_payload, _context):
                observed.append(
                    store.connection.execute(
                        "SELECT state FROM effect_claims"
                    ).fetchone()[0]
                )
                return {"number": 101}

            adapters.register(
                "rappterbook.post.create",
                adapter_id="recording@1",
                validate=lambda _payload: [],
                execute=execute,
            )
            adapters.freeze()
            result = effect_executor.execute_effects_batch(
                receipt,
                expected=expected_batch(receipt),
                registry=adapters,
                store=store,
                namespace="test",
                execution_id="batch-state",
            )
            final_state = store.connection.execute(
                "SELECT state FROM effect_claims"
            ).fetchone()[0]
            store.close()
        self.assertEqual(result["status"], "completed")
        self.assertEqual(observed, ["executing"])
        self.assertEqual(final_state, "applied")

    def test_abort_batch_is_atomic_for_reference_stores(self):
        factories = [
            ("memory", lambda _directory: effect_executor.InMemoryIdempotencyStore()),
            (
                "sqlite",
                lambda directory: effect_executor.SQLiteIdempotencyStore(
                    Path(directory) / "effects.sqlite3"
                ),
            ),
        ]
        reservations = [
            {"key": "first", "fingerprint": "a" * 64},
            {"key": "second", "fingerprint": "b" * 64},
        ]
        for name, factory in factories:
            with self.subTest(store=name), tempfile.TemporaryDirectory() as directory:
                store = factory(directory)
                reserved = store.reserve_batch("test", reservations, "owner")
                first, second = reserved["claims"]
                self.assertTrue(
                    store.begin_batch_execution(
                        reserved["batch_token"],
                        first["token"],
                    )
                )
                self.assertFalse(
                    store.abort_batch(
                        reserved["batch_token"],
                        first["token"],
                        ["wrong"],
                    )
                )
                self.assertTrue(
                    store.abort_batch(
                        reserved["batch_token"],
                        first["token"],
                        [second["token"]],
                    )
                )
                self.assertEqual(
                    store.claim("test", "first", "a" * 64, "next")["decision"],
                    "indeterminate",
                )
                self.assertEqual(
                    store.claim("test", "second", "b" * 64, "next")["decision"],
                    "claimed",
                )
                if hasattr(store, "close"):
                    store.close()

    def test_abort_reserved_batch_releases_every_untouched_claim(self):
        factories = [
            ("memory", lambda _directory: effect_executor.InMemoryIdempotencyStore()),
            (
                "sqlite",
                lambda directory: effect_executor.SQLiteIdempotencyStore(
                    Path(directory) / "effects.sqlite3"
                ),
            ),
        ]
        reservations = [
            {"key": "first", "fingerprint": "a" * 64},
            {"key": "second", "fingerprint": "b" * 64},
        ]
        for name, factory in factories:
            with self.subTest(store=name), tempfile.TemporaryDirectory() as directory:
                store = factory(directory)
                reserved = store.reserve_batch("test", reservations, "owner")
                self.assertTrue(
                    store.abort_reserved_batch(reserved["batch_token"])
                )
                for item in reservations:
                    self.assertEqual(
                        store.claim(
                            "test",
                            item["key"],
                            item["fingerprint"],
                            "next",
                        )["decision"],
                        "claimed",
                    )
                if hasattr(store, "close"):
                    store.close()

    def test_stopped_duplicate_result_respects_aggregate_limit(self):
        receipt = proposal(SOURCE_TWO)
        calls = []
        adapters = registry(calls)
        store = effect_executor.InMemoryIdempotencyStore()
        snapshot = lisp._copy_host_value(receipt)
        digest = effect_executor.proposal_sha256(snapshot)
        entries = [
            adapters.get(effect["type"])
            for effect in snapshot["effects"]
        ]
        reservations = [
            {
                "key": effect["idempotency_key"],
                "fingerprint": effect_executor._effect_fingerprint(
                    effect,
                    entry,
                    snapshot,
                    digest,
                ),
            }
            for effect, entry in zip(snapshot["effects"], entries)
        ]
        reserved = store.reserve_batch("test", reservations, "seed")
        first, second = reserved["claims"]
        self.assertTrue(
            store.begin_batch_execution(
                reserved["batch_token"],
                second["token"],
            )
        )
        store.succeed(second["token"], {"number": 99})
        self.assertTrue(
            store.release_batch(
                reserved["batch_token"],
                [first["token"]],
            )
        )

        failing = effect_executor.EffectAdapterRegistry()
        failing.register(
            "rappterbook.post.create",
            adapter_id="recording@1",
            validate=lambda _payload: [],
            execute=lambda _payload, _context: (_ for _ in ()).throw(
                RuntimeError("failed")
            ),
        )
        failing.freeze()
        result = effect_executor.execute_effects_batch(
            receipt,
            expected=expected_batch(receipt),
            registry=failing,
            store=store,
            namespace="test",
            execution_id="aggregate",
            max_total_result_bytes=5,
        )
        self.assertEqual(result["effects"][0]["status"], "indeterminate")
        self.assertEqual(result["effects"][1]["status"], "duplicate_applied")
        self.assertIsNone(result["effects"][1]["result"])
        self.assertEqual(
            result["effects"][1]["error"]["code"],
            "duplicate_result_omitted",
        )

    def test_batch_late_conflict_calls_no_adapter_and_reserves_nothing(self):
        receipt = proposal(SOURCE_TWO)
        calls = []
        adapters = registry(calls)
        store = effect_executor.InMemoryIdempotencyStore()
        second_key = receipt["effects"][1]["idempotency_key"]
        store.claim("test", second_key, "wrong", "seed")
        result = effect_executor.execute_effects_batch(
            receipt,
            expected=expected_batch(receipt),
            registry=adapters,
            store=store,
            namespace="test",
            execution_id="batch-conflict",
        )
        self.assertEqual(result["status"], "rejected")
        self.assertEqual(
            [item["status"] for item in result["effects"]],
            ["not_reserved", "conflict"],
        )
        self.assertEqual(calls, [])
        first_key = receipt["effects"][0]["idempotency_key"]
        self.assertEqual(
            store.claim("test", first_key, "new", "probe")["decision"],
            "claimed",
        )

    def test_batch_result_limit_marks_current_and_releases_tail(self):
        receipt = proposal(SOURCE_TWO)
        calls = []
        adapters = effect_executor.EffectAdapterRegistry()

        def execute(payload, _context):
            calls.append(payload["title"])
            return "too-large"

        adapters.register(
            "rappterbook.post.create",
            adapter_id="recording@1",
            validate=lambda _payload: [],
            execute=execute,
        )
        adapters.freeze()
        store = effect_executor.InMemoryIdempotencyStore()
        result = effect_executor.execute_effects_batch(
            receipt,
            expected=expected_batch(receipt),
            registry=adapters,
            store=store,
            namespace="test",
            execution_id="batch-limit",
            max_result_bytes=5,
        )
        self.assertEqual(result["status"], "stopped")
        self.assertEqual(
            [item["status"] for item in result["effects"]],
            ["indeterminate", "not_attempted"],
        )
        self.assertEqual(calls, ["First"])
        self.assertEqual(
            [record["state"] for record in store._records.values()],
            ["indeterminate"],
        )

    def test_batch_aggregate_limit_precedes_second_store_success(self):
        receipt = proposal(SOURCE_TWO)
        calls = []
        adapters = effect_executor.EffectAdapterRegistry()

        def execute(payload, _context):
            calls.append(payload["title"])
            return "aaaa"

        adapters.register(
            "rappterbook.post.create",
            adapter_id="recording@1",
            validate=lambda _payload: [],
            execute=execute,
        )
        adapters.freeze()
        store = effect_executor.InMemoryIdempotencyStore()
        result = effect_executor.execute_effects_batch(
            receipt,
            expected=expected_batch(receipt),
            registry=adapters,
            store=store,
            namespace="test",
            execution_id="batch-aggregate",
            max_total_result_bytes=6,
        )
        self.assertEqual(result["status"], "partially_applied")
        self.assertEqual(
            [item["status"] for item in result["effects"]],
            ["applied", "indeterminate"],
        )
        self.assertEqual(calls, ["First", "Second"])
        self.assertEqual(
            sorted(record["state"] for record in store._records.values()),
            ["applied", "indeterminate"],
        )

    def test_batch_restores_v1_identifier_validation(self):
        receipt = proposal()
        with self.assertRaises(effect_executor.EffectExecutionError):
            effect_executor.execute_effects_batch(
                receipt,
                expected=expected_batch(receipt),
                registry=registry([]),
                store=effect_executor.InMemoryIdempotencyStore(),
                namespace="",
                execution_id="batch",
            )
        with self.assertRaises(effect_executor.EffectExecutionError):
            effect_executor.execute_effects_batch(
                receipt,
                expected=expected_batch(receipt),
                registry=registry([]),
                store=effect_executor.InMemoryIdempotencyStore(),
                namespace="test",
                execution_id="",
            )

    def test_sqlite_batch_reservation_replays_after_reopen(self):
        receipt = proposal(SOURCE_TWO)
        calls = []
        adapters = registry(calls)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "batch.sqlite3"
            first_store = effect_executor.SQLiteIdempotencyStore(path)
            first = effect_executor.execute_effects_batch(
                receipt,
                expected=expected_batch(receipt),
                registry=adapters,
                store=first_store,
                namespace="test",
                execution_id="batch-1",
            )
            first_store.close()
            second_store = effect_executor.SQLiteIdempotencyStore(path)
            second = effect_executor.execute_effects_batch(
                receipt,
                expected=expected_batch(receipt),
                registry=adapters,
                store=second_store,
                namespace="test",
                execution_id="batch-2",
            )
            second_store.close()
        self.assertEqual(first["status"], "completed")
        self.assertEqual(
            [item["status"] for item in second["effects"]],
            ["duplicate_applied", "duplicate_applied"],
        )
        self.assertEqual(len(calls), 2)

    def test_batch_release_is_all_or_none_for_both_stores(self):
        factories = [
            ("memory", lambda _directory: effect_executor.InMemoryIdempotencyStore()),
            (
                "sqlite",
                lambda directory: effect_executor.SQLiteIdempotencyStore(
                    Path(directory) / "batch.sqlite3"
                ),
            ),
        ]
        reservations = [
            {"key": "first", "fingerprint": "a" * 64},
            {"key": "second", "fingerprint": "b" * 64},
        ]
        for name, factory in factories:
            with self.subTest(store=name), tempfile.TemporaryDirectory() as directory:
                store = factory(directory)
                reserved = store.reserve_batch("test", reservations, "owner")
                tokens = [item["token"] for item in reserved["claims"]]
                self.assertFalse(
                    store.release_batch(
                        reserved["batch_token"],
                        [tokens[0], "wrong"],
                    )
                )
                self.assertFalse(
                    store.release_batch("wrong", tokens)
                )
                self.assertTrue(
                    store.release_batch(reserved["batch_token"], tokens)
                )
                if hasattr(store, "close"):
                    store.close()

    def test_batch_reports_preexisting_duplicate_after_earlier_failure(self):
        receipt = proposal(SOURCE_TWO)
        seed_calls = []
        adapters = registry(seed_calls)
        store = effect_executor.InMemoryIdempotencyStore()
        snapshot = lisp._copy_host_value(receipt)
        digest = effect_executor.proposal_sha256(snapshot)
        entries = [
            adapters.get(effect["type"])
            for effect in snapshot["effects"]
        ]
        reservations = [
            {
                "key": effect["idempotency_key"],
                "fingerprint": effect_executor._effect_fingerprint(
                    effect,
                    entry,
                    snapshot,
                    digest,
                ),
            }
            for effect, entry in zip(snapshot["effects"], entries)
        ]
        reserved = store.reserve_batch("test", reservations, "seed")
        first, second = reserved["claims"]
        store.begin_batch_execution(
            reserved["batch_token"],
            second["token"],
        )
        store.succeed(second["token"], {"number": 99})
        store.release_batch(reserved["batch_token"], [first["token"]])

        failing = effect_executor.EffectAdapterRegistry()
        failing.register(
            "rappterbook.post.create",
            adapter_id="recording@1",
            validate=lambda _payload: [],
            execute=lambda _payload, _context: (_ for _ in ()).throw(
                RuntimeError("failed")
            ),
        )
        failing.freeze()
        result = effect_executor.execute_effects_batch(
            receipt,
            expected=expected_batch(receipt),
            registry=failing,
            store=store,
            namespace="test",
            execution_id="batch-partial",
        )
        self.assertEqual(result["status"], "partially_applied")
        self.assertEqual(
            [item["status"] for item in result["effects"]],
            ["indeterminate", "duplicate_applied"],
        )
        self.assertEqual(result["effects"][1]["result"], {"number": 99})


if __name__ == "__main__":
    unittest.main()
