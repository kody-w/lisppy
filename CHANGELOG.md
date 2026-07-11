# Changelog

## 0.24.0

- Split the portable Core stdlib contract from optional Rappterbook helpers,
  exposed active profiles explicitly, and expanded boundary/stdlib vectors.
- Added packaged Mars governor contract and policy vectors with deterministic
  local evidence and explicit decision-only/external-unverified semantics.
- Tightened hosted-frame execution receipts, batch-wide malformed-reservation
  cleanup, and fail-closed demo proof checks.

## 0.23.0

- Added a frozen-ABI `LispyRuntime` `*_agent.py` adapter so LisPy rides RAPP as
  userspace instead of claiming a second kernel, route, or runtime-parity tier.
- Added a strict `rapp-cubby/1.0` manifest and machine-verifiable RAPP
  compliance receipt pinned to the latest exhaustive `rapp-spine/1.1` crawl.
- Documented applicable and non-applicable RAPP contracts plus registration,
  dependency-distribution, and upstream-spine completion gaps.

## 0.22.0

- Completed hosted-frame v2 phase semantics with pure effect preflight, strict
  execution-receipt validation, and uncertainty only after executor entry.
- Promoted full bounded corpus/wire validation into contract-pack v2, added
  canonical manifest pins, and normalized verification failures to JSON.
- Verified every applicable installed RECORD row, tightened launcher/bytecode
  paths, and added a separate pre/post-build packaging-input identity.

## 0.21.0

- Froze phase-aware hosted-frame v2 failure receipts so any uncertainty after
  executor entry requires reconciliation while pure preflight failures do not.
- Completed contract-pack v2 with exact Core stdlib bytes, independent public
  verification, corruption rejection, and installed CLI verification gates.
- Closed installed RECORD payloads, pinned pre/post-build source identity, and
  completed Mars policy outputs plus remaining CLI/numeric reliability gaps.

## 0.20.0

- Added self-verifying `lispy.contract-pack/v2` exports containing every exact
  manifest-bound Core 1 resource, while retaining legacy pack v1.
- Made structural equality iterative, maps string-keyed and fail-closed, and
  collection limits comprehensive across strings, maps, JSON and final values.
- Added semantic proposal identity across direct/worker transports, structured
  hosted-frame v2 preflight receipts, and a total Mars governor candidate v2.

## 0.19.0

- Completed fail-closed CLI mode selection, structured JSON usage errors, and
  explicit UTF-8 handling for fixtures, subprocesses, replay, and soul files.
- Shipped an installed, digest-bound `lispy-core@1` contract pack and made
  portable VM results/numeric operations reject Python-only value leakage.
- Added authoritative registered-governor execution and a batch-backed hosted
  frame API; the demo and release effects doctor now exercise executor v2.

## 0.18.0

- Repaired release-text encoding, narrowed external runtime claims to their
  evidence level, and added UTF-8/package integrity gates.
- Made CLI special modes reject ignored flags, bounded script/stdin reads before
  decoding, and completed fail-before-effects and logical-list edge cases.
- Added the installed offline hosted-flow demo and a closed evidence-bearing
  `lispy-examples@2` profile manifest.

## 0.17.0

- Made doctor-v3 failed inventories schema-valid, enforced exact ordered check
  catalogs and evidence identities, and isolated regular component failures.
- Added durable `reserved` and `executing` batch states, atomic batch aborts,
  custom-adapter cleanup, and aggregate accounting for duplicate results.
- Added fail-closed release preflight, TestPyPI server-side digest verification,
  wheel/sdist release doctors, and shared timeout-enforcing test launchers.

## 0.16.0

- Persisted executor-v2 batch tokens and added all-or-none untouched-tail
  release for both reference stores.
- Added strict zero-dependency doctor-v3 report validation and a 64-KiB output
  ceiling.
- Expanded shared subprocess support and immutable replay fixture caching.

## 0.15.0

- Added executor durable-state cleanup and deterministic failure reporting.
- Made doctor-v3 timeout and evidence failures component-local.
- Continued migration to shared subprocess and replay test support.

## 0.14.0

- Hardened executor cleanup and state integrity after partial failures.
- Made doctor-v3 components independently fail closed in one stable report.
- Added shared replay fixtures and corrected disabled-publisher checksum gates.

## 0.13.0

- Added additive atomic-reservation executor v2.
- Added opt-in doctor-v3 inventory, replay, effects, and release profiles.
- Added a disabled, fail-closed OIDC publishing workflow skeleton.

## 0.12.0

- Enforced finite bounded values and stricter successful worker schemas.
- Hardened installed-doctor failure identities and origin checks.
- Added an explicitly inert Core 2 Form/Vector design draft.

## 0.11.0

- Added iterative limits for deeply nested runtime and wire values.
- Corrected worker EOF, timeout, process-group termination, and reap ordering.
- Stabilized fail-closed installed-distribution inventory checks.

## 0.10.0

- Added bounded concurrent worker pipe and numeric-token handling.
- Added installed-wheel RECORD verification and tamper detection.
- Renamed only the distribution to `rappterbook-lispy-runtime`; imports and the
  `lispy` command remain unchanged.

## 0.9.0

- Added named `installed@1`, `effects@1`, and `release@1` doctor profiles.
- Added external replay bundle/artifact pins and independent digest checks.
- Added pre-decode JSON complexity and worker response limits.

## 0.8.0

- Normalized Core 1 logical-list consumers across nil, Python lists, and proper
  Pair chains with cycle/improper-list rejection and consistent limits.
- Hardened JSONL worker shape validation, bounded line draining, private
  per-request directories, response correlation, crash sanitization, and POSIX
  process-group timeout cleanup.
- Completed release artifacts with examples/tests/specs in the sdist, standard
  build ignores, multi-version CI, and wheel rebuilding from the sdist.

## 0.7.0

- Hardened Core 1 truth/equality plus recursive and finite JSON semantics behind
  a centralized `Core1ValueOps` seam.
- Bound replay receipts to a runtime/stdlib/source/validator artifact digest and
  made replay fail closed on runtime, semantic, source-hash, and schema errors.
- Hardened the effect executor with independent proposal pins, immutable
  registries, copied proposal/results, fail-closed store decisions, and stronger
  SQLite transaction handling.

## 0.6.0

- Added the installable `lisppy` facade, `lispy` console command, and packaged
  runtime resources while preserving `python lisp.py` and `import lisp`.
- Added deterministic replay-bundle export/import with digest, manifest,
  registered-source, state-baseline, and response verification.
- Added a host-only typed effect executor with frozen adapters and in-memory or
  SQLite idempotency stores.

### Migration

- The default state root is now `./state`; use `STATE_DIR` or `--state-dir` for
  another root.
- Prefer `from lisppy import LispyVM` for installed applications.
- Effect execution remains a host responsibility and is never exposed to
  untrusted LisPy programs or the JSONL worker.
