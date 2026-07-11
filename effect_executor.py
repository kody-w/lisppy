"""Host-only ordered executor for accepted LisPy dry-run effects."""

import hashlib
import json
import secrets
import sqlite3
import threading
from types import MappingProxyType

import lisp


class EffectExecutionError(RuntimeError):
    pass


class EffectAdapterRegistry:
    def __init__(self):
        self._entries = {}
        self._frozen = False
        self._lock = threading.Lock()

    def register(self, effect_type, *, adapter_id, validate, execute):
        with self._lock:
            if self._frozen:
                raise EffectExecutionError("adapter registry is frozen")
            if effect_type in self._entries:
                raise EffectExecutionError(
                    f"duplicate effect type: {effect_type}"
                )
            self._entries[effect_type] = {
                "adapter_id": adapter_id,
                "validate": validate,
                "execute": execute,
            }

    def freeze(self):
        with self._lock:
            self._entries = MappingProxyType(
                {
                    key: MappingProxyType(dict(value))
                    for key, value in self._entries.items()
                }
            )
            self._frozen = True
        return self

    def get(self, effect_type):
        if effect_type not in self._entries:
            raise EffectExecutionError(f"unknown effect type: {effect_type}")
        return self._entries[effect_type]

    @property
    def frozen(self):
        return self._frozen


class InMemoryIdempotencyStore:
    """Thread-safe test/reference store; not durable."""

    def __init__(self):
        self._records = {}
        self._lock = threading.Lock()

    def claim(self, namespace, key, fingerprint, owner):
        record_key = (namespace, key)
        with self._lock:
            record = self._records.get(record_key)
            if record is None:
                token = _token(namespace, key, fingerprint, owner)
                self._records[record_key] = {
                    "fingerprint": fingerprint,
                    "state": "pending",
                    "token": token,
                    "result": None,
                    "batch_token": None,
                }
                return {"decision": "claimed", "token": token}
            if record["fingerprint"] != fingerprint:
                return {"decision": "conflict"}
            if record["state"] == "applied":
                return {
                    "decision": "duplicate_applied",
                    "result": lisp._copy_host_value(record["result"]),
                }
            return {"decision": "indeterminate"}

    def reserve_batch(self, namespace, reservations, owner):
        keys = [item["key"] for item in reservations]
        if len(keys) != len(set(keys)):
            raise EffectExecutionError("duplicate reservation key")
        with self._lock:
            decisions = []
            staged = {}
            batch_token = secrets.token_hex(32)
            for index, item in enumerate(reservations):
                record_key = (namespace, item["key"])
                record = self._records.get(record_key)
                if record is None:
                    token = _token(
                        namespace,
                        item["key"],
                        item["fingerprint"],
                        owner,
                    )
                    decisions.append(
                        {
                            **item,
                            "decision": "claimed",
                            "token": token,
                        }
                    )
                    staged[record_key] = {
                        "fingerprint": item["fingerprint"],
                        "state": "reserved",
                        "token": token,
                        "result": None,
                        "batch_token": batch_token,
                    }
                elif record["fingerprint"] != item["fingerprint"]:
                    return {"decision": "conflict", "index": index}
                elif record["state"] == "applied":
                    decisions.append(
                        {
                            **item,
                            "decision": "duplicate_applied",
                            "result": lisp._copy_host_value(record["result"]),
                        }
                    )
                else:
                    return {"decision": "indeterminate", "index": index}
            self._records.update(staged)
            return {
                "decision": "reserved",
                "batch_token": batch_token,
                "claims": decisions,
            }

    def succeed(self, token, result):
        with self._lock:
            record = _find_token(self._records.values(), token)
            if (
                record is None
                or record["state"] not in ("pending", "executing")
            ):
                raise EffectExecutionError("idempotency claim is not pending")
            record["state"] = "applied"
            record["result"] = lisp._copy_host_value(result)
            record["batch_token"] = None

    def begin_batch_execution(self, batch_token, token):
        with self._lock:
            record = _find_token(self._records.values(), token)
            if (
                record is None
                or record["state"] not in ("pending", "reserved")
                or record.get("batch_token") != batch_token
            ):
                return False
            record["state"] = "executing"
            return True

    def mark_indeterminate(self, token):
        with self._lock:
            record = _find_token(self._records.values(), token)
            if (
                record is not None
                and record["state"] in ("pending", "reserved", "executing")
            ):
                record["state"] = "indeterminate"
                record["batch_token"] = None

    def release_claim(self, token):
        with self._lock:
            key = next(
                (
                    key
                    for key, record in self._records.items()
                    if record["token"] == token
                    and record["state"] == "pending"
                    and record.get("batch_token") is None
                ),
                None,
            )
            if key is None:
                return False
            del self._records[key]
            return True

    def release_batch(self, batch_token, claim_tokens):
        if len(claim_tokens) != len(set(claim_tokens)):
            return False
        if not claim_tokens:
            return True
        with self._lock:
            keys = []
            for token in claim_tokens:
                key = next(
                    (
                        key
                        for key, record in self._records.items()
                        if record["token"] == token
                        and record["state"] in ("pending", "reserved")
                        and record.get("batch_token") == batch_token
                    ),
                    None,
                )
                if key is None:
                    return False
                keys.append(key)
            for key in keys:
                del self._records[key]
            return True

    def abort_batch(self, batch_token, current_token, tail_tokens):
        if (
            current_token in tail_tokens
            or len(tail_tokens) != len(set(tail_tokens))
        ):
            return False
        with self._lock:
            current = _find_token(self._records.values(), current_token)
            if (
                current is None
                or current["state"] not in ("pending", "reserved", "executing")
                or current.get("batch_token") != batch_token
            ):
                return False
            tail_keys = []
            for token in tail_tokens:
                key = next(
                    (
                        key
                        for key, record in self._records.items()
                        if record["token"] == token
                        and record["state"] in ("pending", "reserved")
                        and record.get("batch_token") == batch_token
                    ),
                    None,
                )
                if key is None:
                    return False
                tail_keys.append(key)
            current["state"] = "indeterminate"
            current["batch_token"] = None
            for key in tail_keys:
                del self._records[key]
            return True

    def abort_reserved_batch(self, batch_token):
        with self._lock:
            matching = [
                key
                for key, record in self._records.items()
                if record.get("batch_token") == batch_token
            ]
            if any(
                self._records[key]["state"] not in ("pending", "reserved")
                for key in matching
            ):
                return False
            for key in matching:
                del self._records[key]
            return True


class SQLiteIdempotencyStore:
    """Durable SQLite store using transactional claims."""

    def __init__(self, path):
        self.connection = sqlite3.connect(path, timeout=30)
        self.connection.execute(
            """
            CREATE TABLE IF NOT EXISTS effect_claims (
                namespace TEXT NOT NULL,
                key TEXT NOT NULL,
                fingerprint TEXT NOT NULL,
                state TEXT NOT NULL,
                token TEXT NOT NULL UNIQUE,
                result_json TEXT,
                batch_token TEXT,
                PRIMARY KEY (namespace, key)
            )
            """
        )
        columns = {
            row[1]
            for row in self.connection.execute(
                "PRAGMA table_info(effect_claims)"
            )
        }
        if "batch_token" not in columns:
            self.connection.execute(
                "ALTER TABLE effect_claims ADD COLUMN batch_token TEXT"
            )
        self.connection.commit()

    def claim(self, namespace, key, fingerprint, owner):
        token = _token(namespace, key, fingerprint, owner)
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            row = self.connection.execute(
                """
                SELECT fingerprint, state, result_json
                FROM effect_claims
                WHERE namespace = ? AND key = ?
                """,
                (namespace, key),
            ).fetchone()
            if row is None:
                self.connection.execute(
                    """
                    INSERT INTO effect_claims
                    (namespace, key, fingerprint, state, token)
                    VALUES (?, ?, ?, 'pending', ?)
                    """,
                    (namespace, key, fingerprint, token),
                )
                decision = {"decision": "claimed", "token": token}
            elif row[0] != fingerprint:
                decision = {"decision": "conflict"}
            elif row[1] == "applied":
                decoded = json.loads(row[2])
                decision = {
                    "decision": "duplicate_applied",
                    "result": lisp._copy_host_value(decoded),
                }
            else:
                decision = {"decision": "indeterminate"}
            self.connection.commit()
            return decision
        except (sqlite3.Error, TypeError, ValueError):
            self.connection.rollback()
            raise

    def reserve_batch(self, namespace, reservations, owner):
        keys = [item["key"] for item in reservations]
        if len(keys) != len(set(keys)):
            raise EffectExecutionError("duplicate reservation key")
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            decisions = []
            staged = []
            batch_token = secrets.token_hex(32)
            for index, item in enumerate(reservations):
                row = self.connection.execute(
                    """
                    SELECT fingerprint, state, result_json
                    FROM effect_claims
                    WHERE namespace = ? AND key = ?
                    """,
                    (namespace, item["key"]),
                ).fetchone()
                if row is None:
                    token = _token(
                        namespace,
                        item["key"],
                        item["fingerprint"],
                        owner,
                    )
                    decisions.append(
                        {
                            **item,
                            "decision": "claimed",
                            "token": token,
                        }
                    )
                    staged.append(
                        (
                            namespace,
                            item["key"],
                            item["fingerprint"],
                            token,
                            batch_token,
                        )
                    )
                elif row[0] != item["fingerprint"]:
                    self.connection.rollback()
                    return {"decision": "conflict", "index": index}
                elif row[1] == "applied":
                    decisions.append(
                        {
                            **item,
                            "decision": "duplicate_applied",
                            "result": lisp._copy_host_value(
                                json.loads(row[2])
                            ),
                        }
                    )
                else:
                    self.connection.rollback()
                    return {"decision": "indeterminate", "index": index}
            self.connection.executemany(
                """
                INSERT INTO effect_claims
                (namespace, key, fingerprint, state, token, batch_token)
                VALUES (?, ?, ?, 'reserved', ?, ?)
                """,
                staged,
            )
            self.connection.commit()
            return {
                "decision": "reserved",
                "batch_token": batch_token,
                "claims": decisions,
            }
        except (
            EffectExecutionError,
            sqlite3.Error,
            TypeError,
            ValueError,
        ):
            self.connection.rollback()
            raise

    def succeed(self, token, result):
        result = lisp._copy_host_value(result)
        encoded = json.dumps(
            result,
            allow_nan=False,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        try:
            cursor = self.connection.execute(
                """
                UPDATE effect_claims
                SET state = 'applied', result_json = ?, batch_token = NULL
                WHERE token = ? AND state IN ('pending', 'executing')
                """,
                (encoded, token),
            )
            if cursor.rowcount != 1:
                raise EffectExecutionError(
                    "idempotency claim is not pending"
                )
            self.connection.commit()
        except (EffectExecutionError, sqlite3.Error):
            self.connection.rollback()
            raise

    def begin_batch_execution(self, batch_token, token):
        try:
            cursor = self.connection.execute(
                """
                UPDATE effect_claims
                SET state = 'executing'
                WHERE token = ? AND batch_token = ?
                AND state IN ('pending', 'reserved')
                """,
                (token, batch_token),
            )
            self.connection.commit()
            return cursor.rowcount == 1
        except sqlite3.Error:
            self.connection.rollback()
            raise

    def mark_indeterminate(self, token):
        try:
            self.connection.execute(
                """
                UPDATE effect_claims
                SET state = 'indeterminate', batch_token = NULL
                WHERE token = ?
                AND state IN ('pending', 'reserved', 'executing')
                """,
                (token,),
            )
            self.connection.commit()
        except sqlite3.Error:
            self.connection.rollback()
            raise

    def release_claim(self, token):
        try:
            cursor = self.connection.execute(
                """
                DELETE FROM effect_claims
                WHERE token = ? AND state = 'pending'
                AND batch_token IS NULL
                """,
                (token,),
            )
            self.connection.commit()
            return cursor.rowcount == 1
        except sqlite3.Error:
            self.connection.rollback()
            raise

    def release_batch(self, batch_token, claim_tokens):
        if len(claim_tokens) != len(set(claim_tokens)):
            return False
        if not claim_tokens:
            return True
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            placeholders = ",".join("?" for _ in claim_tokens)
            rows = self.connection.execute(
                f"""
                SELECT token
                FROM effect_claims
                WHERE batch_token = ?
                AND state IN ('pending', 'reserved')
                AND token IN ({placeholders})
                """,
                (batch_token, *claim_tokens),
            ).fetchall()
            if {row[0] for row in rows} != set(claim_tokens):
                self.connection.rollback()
                return False
            cursor = self.connection.execute(
                f"""
                DELETE FROM effect_claims
                WHERE batch_token = ?
                AND state IN ('pending', 'reserved')
                AND token IN ({placeholders})
                """,
                (batch_token, *claim_tokens),
            )
            if cursor.rowcount != len(claim_tokens):
                self.connection.rollback()
                return False
            self.connection.commit()
            return True
        except sqlite3.Error:
            self.connection.rollback()
            raise

    def abort_batch(self, batch_token, current_token, tail_tokens):
        if (
            current_token in tail_tokens
            or len(tail_tokens) != len(set(tail_tokens))
        ):
            return False
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            cursor = self.connection.execute(
                """
                UPDATE effect_claims
                SET state = 'indeterminate', batch_token = NULL
                WHERE token = ? AND batch_token = ?
                AND state IN ('pending', 'reserved', 'executing')
                """,
                (current_token, batch_token),
            )
            if cursor.rowcount != 1:
                self.connection.rollback()
                return False
            if tail_tokens:
                placeholders = ",".join("?" for _ in tail_tokens)
                rows = self.connection.execute(
                    f"""
                    SELECT token
                    FROM effect_claims
                    WHERE batch_token = ?
                    AND state IN ('pending', 'reserved')
                    AND token IN ({placeholders})
                    """,
                    (batch_token, *tail_tokens),
                ).fetchall()
                if {row[0] for row in rows} != set(tail_tokens):
                    self.connection.rollback()
                    return False
                deleted = self.connection.execute(
                    f"""
                    DELETE FROM effect_claims
                    WHERE batch_token = ?
                    AND state IN ('pending', 'reserved')
                    AND token IN ({placeholders})
                    """,
                    (batch_token, *tail_tokens),
                )
                if deleted.rowcount != len(tail_tokens):
                    self.connection.rollback()
                    return False
            self.connection.commit()
            return True
        except sqlite3.Error:
            self.connection.rollback()
            raise

    def abort_reserved_batch(self, batch_token):
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            unsafe = self.connection.execute(
                """
                SELECT COUNT(*)
                FROM effect_claims
                WHERE batch_token = ?
                AND state NOT IN ('pending', 'reserved')
                """,
                (batch_token,),
            ).fetchone()[0]
            if unsafe:
                self.connection.rollback()
                return False
            self.connection.execute(
                """
                DELETE FROM effect_claims
                WHERE batch_token = ?
                AND state IN ('pending', 'reserved')
                """,
                (batch_token,),
            )
            self.connection.commit()
            return True
        except sqlite3.Error:
            self.connection.rollback()
            raise

    def close(self):
        self.connection.close()


def execute_effects(
    proposal,
    *,
    expected,
    registry,
    store,
    namespace,
    execution_id,
):
    """Execute an accepted proposal in order with fail-stop semantics."""
    if not isinstance(namespace, str) or not namespace:
        raise EffectExecutionError("namespace must be a non-empty string")
    if not isinstance(execution_id, str) or not execution_id:
        raise EffectExecutionError("execution_id must be a non-empty string")
    if not isinstance(expected, dict):
        raise EffectExecutionError("expected pins must be an object")
    snapshot = lisp._copy_host_value(proposal)
    proposal_digest = proposal_sha256(snapshot)
    if expected.get("proposal_sha256") != proposal_digest:
        raise EffectExecutionError("proposal SHA-256 mismatch")
    effects = _preflight(snapshot, expected, registry)
    outcomes = []
    stopped = False

    for effect in effects:
        if stopped:
            entry = registry.get(effect["type"])
            outcomes.append(
                _outcome(
                    effect,
                    "not_attempted",
                    adapter_id=entry["adapter_id"],
                )
            )
            continue
        entry = registry.get(effect["type"])
        fingerprint = _effect_fingerprint(
            effect,
            entry,
            snapshot,
            proposal_digest,
        )
        try:
            claim = store.claim(
                namespace,
                effect["idempotency_key"],
                fingerprint,
                execution_id,
            )
        except (EffectExecutionError, OSError, sqlite3.Error, TypeError, ValueError):
            outcomes.append(
                _outcome(
                    effect,
                    "indeterminate",
                    adapter_id=entry["adapter_id"],
                    error={"code": "store_claim_failed"},
                )
            )
            stopped = True
            continue
        if (
            not isinstance(claim, dict)
            or claim.get("decision")
            not in {
                "claimed",
                "duplicate_applied",
                "conflict",
                "indeterminate",
            }
        ):
            outcomes.append(
                _outcome(
                    effect,
                    "indeterminate",
                    adapter_id=entry["adapter_id"],
                    error={"code": "invalid_store_decision"},
                )
            )
            stopped = True
            continue
        decision = claim["decision"]
        if decision == "duplicate_applied":
            outcomes.append(
                _outcome(
                    effect,
                    "duplicate_applied",
                    adapter_id=entry["adapter_id"],
                    result=lisp._copy_host_value(claim.get("result")),
                )
            )
            continue
        if decision in ("conflict", "indeterminate"):
            outcomes.append(
                _outcome(effect, decision, adapter_id=entry["adapter_id"])
            )
            stopped = True
            continue

        token = claim.get("token")
        if not isinstance(token, str) or not token:
            outcomes.append(
                _outcome(
                    effect,
                    "indeterminate",
                    adapter_id=entry["adapter_id"],
                    error={"code": "invalid_claim_token"},
                )
            )
            stopped = True
            continue
        context = {
            "execution_id": execution_id,
            "sequence": effect["sequence"],
            "idempotency_key": effect["idempotency_key"],
        }
        try:
            result = entry["execute"](
                lisp._copy_host_value(effect["payload"]),
                context,
            )
            public_result = lisp._copy_host_value(result)
            store.succeed(token, public_result)
            outcomes.append(
                _outcome(
                    effect,
                    "applied",
                    adapter_id=entry["adapter_id"],
                    result=public_result,
                )
            )
        except Exception:
            cleanup_ok = True
            try:
                store.mark_indeterminate(token)
            except Exception:
                cleanup_ok = False
            outcomes.append(
                _outcome(
                    effect,
                    "indeterminate",
                    adapter_id=entry["adapter_id"],
                    error={
                        "code": (
                            "adapter_indeterminate"
                            if cleanup_ok
                            else "adapter_cleanup_failed"
                        )
                    },
                )
            )
            stopped = True

    status = "completed"
    if any(item["status"] == "conflict" for item in outcomes):
        status = (
            "partially_applied"
            if any(
                item["status"] in ("applied", "duplicate_applied")
                for item in outcomes
            )
            else "rejected"
        )
    elif stopped:
        status = (
            "partially_applied"
            if any(
                item["status"] in ("applied", "duplicate_applied")
                for item in outcomes
            )
            else "stopped"
        )
    receipt = {
        "api": "lispy.effects-execution/v1",
        "execution_id": execution_id,
        "namespace": namespace,
        "proposal_sha256": proposal_digest,
        "status": status,
        "effects": outcomes,
    }
    json.dumps(receipt, allow_nan=False)
    return receipt


def execute_effects_batch(
    proposal,
    *,
    expected,
    registry,
    store,
    namespace,
    execution_id,
    max_effects=100,
    max_result_bytes=1_048_576,
    max_total_result_bytes=2_097_152,
    max_result_depth=64,
    max_result_nodes=100_000,
):
    """Execute effects after an all-or-none store reservation."""
    if not isinstance(namespace, str) or not namespace:
        raise EffectExecutionError("namespace must be a non-empty string")
    if not isinstance(execution_id, str) or not execution_id:
        raise EffectExecutionError("execution_id must be a non-empty string")
    if not isinstance(expected, dict):
        raise EffectExecutionError("expected pins must be an object")
    for name, value in (
        ("max_effects", max_effects),
        ("max_result_bytes", max_result_bytes),
        ("max_total_result_bytes", max_total_result_bytes),
        ("max_result_depth", max_result_depth),
        ("max_result_nodes", max_result_nodes),
    ):
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise EffectExecutionError(f"{name} must be a positive integer")
    if not hasattr(store, "reserve_batch"):
        raise EffectExecutionError("store does not support atomic reservation")

    snapshot = lisp._copy_host_value(proposal)
    proposal_digest = proposal_sha256(snapshot)
    if expected.get("proposal_sha256") != proposal_digest:
        raise EffectExecutionError("proposal SHA-256 mismatch")
    effects = _preflight(snapshot, expected, registry)
    if len(effects) > max_effects:
        raise EffectExecutionError("proposal exceeds effect limit")
    adapter_ids = {
        effect_type: registry.get(effect_type)["adapter_id"]
        for effect_type in {effect["type"] for effect in effects}
    }
    if expected.get("namespace") != namespace:
        raise EffectExecutionError("authority namespace mismatch")
    if expected.get("adapter_ids") != adapter_ids:
        raise EffectExecutionError("authority adapter mapping mismatch")
    authority = lisp._copy_host_value({
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
    })
    authority_sha256 = _sha256(authority)

    reservations = []
    entries = []
    for effect in effects:
        entry = registry.get(effect["type"])
        entries.append(entry)
        reservations.append(
            {
                "key": effect["idempotency_key"],
                "fingerprint": _effect_fingerprint(
                    effect,
                    entry,
                    snapshot,
                    proposal_digest,
                ),
            }
        )
    try:
        reservation = store.reserve_batch(
            namespace,
            reservations,
            execution_id,
        )
    except Exception as exc:
        raise EffectExecutionError("atomic reservation failed") from exc

    decision = reservation.get("decision") if isinstance(reservation, dict) else None
    if decision in ("conflict", "indeterminate"):
        blocked_index = reservation.get("index")
        outcomes = [
            _outcome(
                effect,
                decision if index == blocked_index else "not_reserved",
                adapter_id=entries[index]["adapter_id"],
            )
            for index, effect in enumerate(effects)
        ]
        return _batch_receipt(
            execution_id,
            namespace,
            proposal_digest,
            authority,
            authority_sha256,
            decision,
            outcomes,
            status="rejected",
        )
    if decision != "reserved":
        raise EffectExecutionError("invalid atomic reservation response")
    batch_token = reservation.get("batch_token")
    if not isinstance(batch_token, str) or not batch_token:
        raise EffectExecutionError("invalid atomic reservation batch token")
    claims = reservation.get("claims")
    try:
        if not isinstance(claims, list) or len(claims) != len(effects):
            raise EffectExecutionError("invalid atomic reservation claims")
        for claim, request in zip(claims, reservations):
            if (
                not isinstance(claim, dict)
                or claim.get("key") != request["key"]
                or claim.get("fingerprint") != request["fingerprint"]
                or claim.get("decision")
                not in ("claimed", "duplicate_applied")
            ):
                raise EffectExecutionError("invalid atomic reservation claim")
            if claim["decision"] == "claimed" and (
                not isinstance(claim.get("token"), str) or not claim["token"]
            ):
                raise EffectExecutionError(
                    "invalid atomic reservation token"
                )
        tokens = [
            claim["token"]
            for claim in claims
            if claim["decision"] == "claimed"
        ]
        if len(tokens) != len(set(tokens)):
            raise EffectExecutionError("duplicate atomic reservation token")
    except EffectExecutionError:
        abort_reserved = getattr(store, "abort_reserved_batch", None)
        if callable(abort_reserved):
            try:
                if not abort_reserved(batch_token):
                    raise EffectExecutionError(
                        "atomic reservation cleanup was rejected"
                    )
            except Exception as cleanup_error:
                raise EffectExecutionError(
                    "invalid atomic reservation cleanup failed"
                ) from cleanup_error
        elif isinstance(claims, list) and hasattr(store, "release_batch"):
            tokens = [
                claim.get("token")
                for claim in claims
                if isinstance(claim, dict)
                and isinstance(claim.get("token"), str)
            ]
            try:
                if not store.release_batch(batch_token, tokens):
                    raise EffectExecutionError(
                        "atomic reservation cleanup was rejected"
                    )
            except Exception as cleanup_error:
                raise EffectExecutionError(
                    "invalid atomic reservation cleanup failed"
                ) from cleanup_error
        raise

    outcomes = []
    total_result_bytes = 0
    stopped = False
    released_tail_tokens = set()
    failed_tail_tokens = set()
    for index, (effect, entry, claim) in enumerate(
        zip(effects, entries, claims)
    ):
        if stopped:
            if claim["decision"] == "duplicate_applied":
                try:
                    duplicate_result, size = _bounded_public_result(
                        claim.get("result"),
                        max_result_bytes,
                        max_result_depth,
                        max_result_nodes,
                    )
                    candidate_total = total_result_bytes + size
                    if candidate_total > max_total_result_bytes:
                        outcomes.append(
                            _outcome(
                                effect,
                                "duplicate_applied",
                                adapter_id=entry["adapter_id"],
                                error={
                                    "code": "duplicate_result_omitted",
                                },
                            )
                        )
                        continue
                    total_result_bytes = candidate_total
                    outcomes.append(
                        _outcome(
                            effect,
                            "duplicate_applied",
                            adapter_id=entry["adapter_id"],
                            result=duplicate_result,
                        )
                    )
                except (
                    EffectExecutionError,
                    lisp.LispError,
                    TypeError,
                    ValueError,
                ):
                    outcomes.append(
                        _outcome(
                            effect,
                            "duplicate_applied",
                            adapter_id=entry["adapter_id"],
                            error={"code": "duplicate_result_invalid"},
                        )
                    )
                continue
            if claim["token"] in failed_tail_tokens:
                outcomes.append(
                    _outcome(
                        effect,
                        "indeterminate",
                        adapter_id=entry["adapter_id"],
                        error={"code": "reservation_release_failed"},
                    )
                )
            elif claim["token"] in released_tail_tokens:
                outcomes.append(
                    _outcome(
                        effect,
                        "not_attempted",
                        adapter_id=entry["adapter_id"],
                    )
                )
            else:
                outcomes.append(
                    _outcome(
                        effect,
                        "indeterminate",
                        adapter_id=entry["adapter_id"],
                        error={"code": "reservation_state_unknown"},
                    )
                )
            continue
        try:
            if claim["decision"] == "duplicate_applied":
                try:
                    public_result, size = _bounded_public_result(
                        claim.get("result"),
                        max_result_bytes,
                        max_result_depth,
                        max_result_nodes,
                    )
                except (
                    EffectExecutionError,
                    lisp.LispError,
                    TypeError,
                    ValueError,
                ):
                    outcomes.append(
                        _outcome(
                            effect,
                            "duplicate_applied",
                            adapter_id=entry["adapter_id"],
                            error={"code": "duplicate_result_invalid"},
                        )
                    )
                    continue
                status = "duplicate_applied"
            else:
                begin_execution = getattr(
                    store,
                    "begin_batch_execution",
                    None,
                )
                if callable(begin_execution) and not begin_execution(
                    batch_token,
                    claim["token"],
                ):
                    raise EffectExecutionError(
                        "batch execution transition failed"
                    )
                context = {
                    "execution_id": execution_id,
                    "sequence": effect["sequence"],
                    "idempotency_key": effect["idempotency_key"],
                }
                result = entry["execute"](
                    lisp._copy_host_value(effect["payload"]),
                    context,
                )
                public_result, size = _bounded_public_result(
                    result,
                    max_result_bytes,
                    max_result_depth,
                    max_result_nodes,
                )
                status = "applied"
            candidate_total = total_result_bytes + size
            if candidate_total > max_total_result_bytes:
                if claim["decision"] == "duplicate_applied":
                    outcomes.append(
                        _outcome(
                            effect,
                            "duplicate_applied",
                            adapter_id=entry["adapter_id"],
                            error={"code": "duplicate_result_omitted"},
                        )
                    )
                    continue
                raise EffectExecutionError("aggregate result limit exceeded")
            if claim["decision"] == "claimed":
                store.succeed(claim["token"], public_result)
            total_result_bytes = candidate_total
            outcomes.append(
                _outcome(
                    effect,
                    status,
                    adapter_id=entry["adapter_id"],
                    result=public_result,
                )
            )
        except Exception:
            tail_tokens = [
                item["token"]
                for item in claims[index + 1 :]
                if item["decision"] == "claimed"
            ]
            cleanup_ok = False
            if claim["decision"] == "claimed":
                abort_batch = getattr(store, "abort_batch", None)
                if callable(abort_batch):
                    try:
                        cleanup_ok = bool(
                            abort_batch(
                                batch_token,
                                claim["token"],
                                tail_tokens,
                            )
                        )
                    except Exception:
                        cleanup_ok = False
                if cleanup_ok:
                    released_tail_tokens.update(tail_tokens)
                else:
                    failed_tail_tokens.update(tail_tokens)
                    fallback_ok = True
                    for token in [claim["token"], *tail_tokens]:
                        try:
                            store.mark_indeterminate(token)
                        except Exception:
                            fallback_ok = False
                    cleanup_ok = fallback_ok
            elif tail_tokens:
                try:
                    cleanup_ok = bool(
                        store.release_batch(batch_token, tail_tokens)
                    )
                except Exception:
                    cleanup_ok = False
                if cleanup_ok:
                    released_tail_tokens.update(tail_tokens)
                else:
                    failed_tail_tokens.update(tail_tokens)
                    fallback_ok = True
                    for token in tail_tokens:
                        try:
                            store.mark_indeterminate(token)
                        except Exception:
                            fallback_ok = False
                    cleanup_ok = fallback_ok
            outcomes.append(
                _outcome(
                    effect,
                    "indeterminate",
                    adapter_id=entry["adapter_id"],
                    error={
                        "code": (
                            "adapter_result_indeterminate"
                            if cleanup_ok
                            else "adapter_result_cleanup_failed"
                        )
                    },
                )
            )
            stopped = True

    partially_applied = any(
        item["status"] in ("applied", "duplicate_applied")
        for item in outcomes
    ) and stopped
    status = "partially_applied" if partially_applied else (
        "stopped" if stopped else "completed"
    )
    return _batch_receipt(
        execution_id,
        namespace,
        proposal_digest,
        authority,
        authority_sha256,
        "reserved",
        outcomes,
        status=status,
    )


def _bounded_public_result(value, max_bytes, max_depth, max_nodes):
    lisp._validate_value_graph(
        value,
        max_depth=max_depth,
        max_nodes=max_nodes,
    )
    copied = lisp._copy_host_value(value)
    encoded = json.dumps(
        copied,
        allow_nan=False,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    if len(encoded) > max_bytes:
        raise EffectExecutionError("adapter result exceeds byte limit")
    return copied, len(encoded)


def _batch_receipt(
    execution_id,
    namespace,
    proposal_digest,
    authority,
    authority_sha256,
    reservation,
    outcomes,
    *,
    status,
):
    receipt = {
        "api": "lispy.effects-execution/v2",
        "execution_id": execution_id,
        "namespace": namespace,
        "proposal_sha256": proposal_digest,
        "authority": authority,
        "authority_sha256": authority_sha256,
        "reservation": reservation,
        "status": status,
        "effects": outcomes,
    }
    json.dumps(receipt, allow_nan=False)
    return receipt


def _preflight(proposal, expected, registry):
    if not registry.frozen:
        raise EffectExecutionError("adapter registry must be frozen")
    if proposal.get("api") != "lispy.hosted-governor/v2":
        raise EffectExecutionError("unsupported proposal API")
    if proposal.get("status") != "accepted" or proposal.get("error") is not None:
        raise EffectExecutionError("proposal is not accepted")
    for name in ("source_id", "source_sha256", "contract_id", "intent_scope"):
        if not isinstance(expected.get(name), str) or not expected[name]:
            raise EffectExecutionError(f"expected {name} must be pinned")
        if proposal.get(name) != expected.get(name):
            raise EffectExecutionError(f"proposal {name} mismatch")
    effects = proposal.get("effects")
    if not isinstance(effects, list):
        raise EffectExecutionError("proposal effects must be a list")

    seen_keys = set()
    for sequence, effect in enumerate(effects):
        required = {
            "sequence",
            "type",
            "payload",
            "mode",
            "applied",
            "idempotency_key",
            "effect_sha256",
        }
        if not isinstance(effect, dict) or set(effect) != required:
            raise EffectExecutionError("invalid effect fields")
        if effect["sequence"] != sequence:
            raise EffectExecutionError("effect sequence must be contiguous")
        if effect["mode"] != "dry_run" or effect["applied"] is not False:
            raise EffectExecutionError("effect is not a dry-run proposal")
        expected_key = lisp.effect_idempotency_key(
            proposal["contract_id"],
            proposal["intent_scope"],
            sequence,
        )
        if effect["idempotency_key"] != expected_key:
            raise EffectExecutionError("effect idempotency key mismatch")
        expected_digest = lisp.effect_digest(
            proposal["source_sha256"],
            effect["type"],
            effect["payload"],
        )
        if effect["effect_sha256"] != expected_digest:
            raise EffectExecutionError("effect digest mismatch")
        if effect["idempotency_key"] in seen_keys:
            raise EffectExecutionError("duplicate idempotency key")
        seen_keys.add(effect["idempotency_key"])
        entry = registry.get(effect["type"])
        violations = entry["validate"](
            lisp._copy_host_value(effect["payload"])
        )
        if violations:
            raise EffectExecutionError(
                "invalid effect payload: " + "; ".join(violations)
            )
    return effects


def proposal_sha256(proposal):
    return _sha256(proposal_core(proposal))


PROPOSAL_CORE_FIELDS = {
    "api",
    "runtime",
    "source_id",
    "source_sha256",
    "contract_id",
    "intent_scope",
    "status",
    "outputs",
    "writes",
    "result",
    "logs",
    "usage",
    "effects",
    "error",
}


def proposal_core(proposal):
    if not isinstance(proposal, dict):
        raise EffectExecutionError("proposal must be an object")
    missing = PROPOSAL_CORE_FIELDS - set(proposal)
    if missing:
        raise EffectExecutionError(
            "proposal core is missing fields: "
            + ", ".join(sorted(missing))
        )
    return lisp._copy_host_value(
        {
            key: proposal[key]
            for key in sorted(PROPOSAL_CORE_FIELDS)
        }
    )


def _effect_fingerprint(effect, entry, proposal, proposal_digest):
    return _sha256(
        {
            "sequence": effect["sequence"],
            "type": effect["type"],
            "payload": effect["payload"],
            "adapter_id": entry["adapter_id"],
            "effect_sha256": effect["effect_sha256"],
            "source_sha256": proposal["source_sha256"],
            "proposal_sha256": proposal_digest,
        }
    )


def _outcome(effect, status, *, adapter_id=None, result=None, error=None):
    return {
        "sequence": effect["sequence"],
        "type": effect["type"],
        "idempotency_key": effect["idempotency_key"],
        "effect_sha256": effect["effect_sha256"],
        "adapter_id": adapter_id,
        "status": status,
        "result": result,
        "error": error,
    }


def _token(namespace, key, fingerprint, owner):
    return secrets.token_hex(32)


def _find_token(records, token):
    return next(
        (record for record in records if record["token"] == token),
        None,
    )


def _sha256(value):
    return hashlib.sha256(
        json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
