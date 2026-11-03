# LisPy vocabulary — one name, one spec, every other dialect has a status

Ruling (Kody Wildfeuer, 2026-11-03): LisPy is locked in as Wildhaven Homes' universal agent language, *now*,
while there are few users, so there is never a JavaScript/ECMAScript split. The code is MIT; the name
**LisPy**, the profile tokens (`lispy-core@1`, `lispy-conformance@2`, `lispy-value@1`, `lispy-agent@1`) and the
conformance mark belong to Wildhaven Homes LLC. An implementation may call itself LisPy only if it passes
`lispy-conformance@2`. Statuses follow the RAPP vocabulary rule: **current · staging · alias of X · retiring** —
nothing is deleted; every published URL and file keeps working.

## The one LisPy

| Thing | Status | Where |
|---|---|---|
| `lispy-core@1` — the language | **current, frozen** (never changes again; anything new is `@2`) | `spec/v1/`, reference `lisp.py` |
| `lispy-conformance@2` — the corpus | **current; the corpus IS the spec** | `spec/v1/conformance.json`, `spec/CONFORMANCE.md` |
| `lispy-value@1` — the value wire | current | `spec/CONFORMANCE.md` |
| `.lispy` — the file extension | **current, canonical** (`.lisp` is read as an alias, never written) | everywhere |
| `lisp.py` — reference implementation | current; embed it **pinned by sha256** | this repo, `python -m lisppy` |
| Determinism | **hard rule**: no `random`, clock, filesystem or network reachable from core/default; only through the one `(effect ...)` syscall with the grant recorded | `lisp.py` (`random` is unbound; `read-file`/`rb-run` are trusted-only) |
| Identity of a program | hash of the **canonical form** (`python -m lisppy --identity file.lispy`), never of raw bytes, so reformatting never mints a new lens | `lisp.py` |
| `do` | **hard error** in core (the single most-drifted form; an error is the only thing that keeps it out) | `lisp.py` |
| `MAINTAINERS` + PR-only writes | governance | `MAINTAINERS` |
| Other hosts (browser, phone, Rust, C) | **like water**: an implementation is *derived* by an AI from `llms.txt` + the corpus in the host's own language and is LisPy only when it passes `lispy-conformance@2`; never hand-ported; Pyodide runs `lisp.py` unchanged in the browser meanwhile | this table |

## Brought in from the other dialects (they earned it)

| From | What | Status |
|---|---|---|
| Brainstem Zaps, Rappterbook SDK | the agent contract `agent-name` / `agent-description` / `agent-parameters` / `(agent-run context kwargs)` — the `.lispy` twin of `BasicAgent.metadata + perform()` | **staging** as `lispy-agent@1` (profile over core) |
| Brainstem Zaps | the single syscall `(effect "name" "ver" "op" args)` behind a default-deny capability kernel, grants and limits frozen into the record | **staging** for the hosted profile; replaces `rb-post/comment/react` as *the* effect model |
| Rappterbook (RappterLisp), Mars vOS `repeat` | `when`, `unless`, `dotimes` | **current** as stdlib macros (no interpreter change) |
| Rappterbook `tock.py` | evaluating fenced ```` ```lispy ```` blocks inside chat posts under the gate | host pattern, documented, not a language feature |
| RAPPterverse | step budget per tick and the 20 Hz reflex loop | host scheduling pattern; the *language* budget is `ExecutionLimits` |

## Retiring (status only; files and URLs stay)

| Dialect | Where | Status | Migration |
|---|---|---|---|
| RAPPterverse reflex VM (Python `scripts/lisp_vm.py`: `do`, `let` = `let*`, no `lambda`; JS `rappter-vm.js`: `fn`/`def`) | kody-w/rappterverse | **retiring** as `rappter-reflex@1` — *not LisPy* | mechanical: `do→begin`, `fn→lambda`, `def→define`, sequential `let→let*`; its 210 compiled routines are data and translate under their own tests |
| Mars Barn vOS dialect (`true` as cond default, flat `(let (x 1 y 2))`, `_` placeholder lambdas, `browser-*` builtins, `(random)`) | `LISPY.md` here; kody-w/mars-barn-opus `docs/os.html` | **retiring** as `mars-vos@1` — *not LisPy* | `browser-*` become a host binding set if Mars Barn still runs; `_` placeholders → explicit `lambda`; `random` → an effect |
| RappterLisp (`eval`, `live_mode` real posts, `curl-post`, `do` → nil) | kody-w/rappterbook `scripts/brainstem/lispy.py` | **retiring** | network and `eval` from inside the language are forbidden by the gate model; keep only its agent-loader shape (now `lispy-agent@1`) |
| lisppy-shepherd's vendored 0.6 core | kody-w/lisppy-shepherd | **retired** (archived) | none |
| `rb-*` in the *default* profile | this repo | **alias of** the `rappterbook` binding profile (demoted out of core) | hosts opt in |
| `examples/*.lisp` | this repo | **alias of** `examples/*.lispy` | renamed; `.lisp` still parses |

## Admission rule (how anything enters core)

A form enters `lispy-core@N+1` only with a year of measured use in RAR (use counts in the static API) and a corpus
entry; core may stay at `@1` forever. Internal library files (`stdlib.lisp`, `core-stdlib.lisp`, packaged data) keep
their `.lisp` names until the deletion pass; only user-facing programs are `.lispy`. Known wart kept on purpose:
`nil` (false, empty) and `()` (the empty list, true) are two values in the frozen corpus; `llms.txt` states it.

## Not built, on purpose (where languages drowned)

No package/module system (eggs by hash in RAR already carry code; left-pad), no second implementation until the
browser needs one, no type system, no async, no bytecode, no macros in frozen records, no metering-in-record until
a second host exists to disagree with the first.

Precedents paid for by others: SQL-92 (compliance without a suite meant nothing) and test262 (JavaScript
converged only when the suite was the definition); Rust editions; Ethereum gas / Erlang reductions / WASM fuel;
E / Caja / WASM / Deno (no ambient authority); JCS / WASM canonical form; Rust's single-compiler decade vs Go's
two; Scheme's `.scm/.ss/.sls` extension mess vs Python's one `.py`.

## Hosting extraction (since 0.26.0)

| Name | Status | Since |
|---|---|---|
| `lispy_hosted.py` | current; optional hosting adapter | 0.26.0 |
| `_dispatch_hosted_mode` | current; hosted CLI dispatch | 0.26.0 |
| `HostedValidationError` | alias of `lispy_hosted.HostedValidationError` | 0.26.0 |
| `_state_path` | alias of `lispy_hosted._state_path` | 0.26.0 |
| `rb_state` | alias of `lispy_hosted.rb_state` | 0.26.0 |
| `rb_agent` | alias of `lispy_hosted.rb_agent` | 0.26.0 |
| `rb_soul` | alias of `lispy_hosted.rb_soul` | 0.26.0 |
| `rb_channels` | alias of `lispy_hosted.rb_channels` | 0.26.0 |
| `rb_trending` | alias of `lispy_hosted.rb_trending` | 0.26.0 |
| `_required_text` | alias of `lispy_hosted._required_text` | 0.26.0 |
| `rb_post` | alias of `lispy_hosted.rb_post` | 0.26.0 |
| `rb_comment` | alias of `lispy_hosted.rb_comment` | 0.26.0 |
| `rb_react` | alias of `lispy_hosted.rb_react` | 0.26.0 |
| `effect_idempotency_key` | alias of `lispy_hosted.effect_idempotency_key` | 0.26.0 |
| `effect_digest` | alias of `lispy_hosted.effect_digest` | 0.26.0 |
| `rb_run` | alias of `lispy_hosted.rb_run` | 0.26.0 |
| `_read_file` | alias of `lispy_hosted._read_file` | 0.26.0 |
| `_write_file` | alias of `lispy_hosted._write_file` | 0.26.0 |
| `_copy_host_value` | alias of `lispy_hosted._copy_host_value` | 0.26.0 |
| `_receipt_value` | alias of `lispy_hosted._receipt_value` | 0.26.0 |
| `_hosted_error_receipt` | alias of `lispy_hosted._hosted_error_receipt` | 0.26.0 |
| `run_hosted_governor` | alias of `lispy_hosted.run_hosted_governor` | 0.26.0 |
| `WORKER_API` | alias of `lispy_hosted.WORKER_API` | 0.26.0 |
| `WORKER_MAX_LINE_BYTES` | alias of `lispy_hosted.WORKER_MAX_LINE_BYTES` | 0.26.0 |
| `WORKER_MAX_RESPONSE_BYTES` | alias of `lispy_hosted.WORKER_MAX_RESPONSE_BYTES` | 0.26.0 |
| `WORKER_MAX_STDERR_BYTES` | alias of `lispy_hosted.WORKER_MAX_STDERR_BYTES` | 0.26.0 |
| `WORKER_TIMEOUT_SECONDS` | alias of `lispy_hosted.WORKER_TIMEOUT_SECONDS` | 0.26.0 |
| `DOCTOR_V3_MAX_REPORT_BYTES` | alias of `lispy_hosted.DOCTOR_V3_MAX_REPORT_BYTES` | 0.26.0 |
| `_protocol_error` | alias of `lispy_hosted._protocol_error` | 0.26.0 |
| `_validate_worker_response_shape` | alias of `lispy_hosted._validate_worker_response_shape` | 0.26.0 |
| `_validate_request_fields` | alias of `lispy_hosted._validate_request_fields` | 0.26.0 |
| `_worker_limits` | alias of `lispy_hosted._worker_limits` | 0.26.0 |
| `_validate_mars_governor` | alias of `lispy_hosted._validate_mars_governor` | 0.26.0 |
| `_registered_sources` | alias of `lispy_hosted._registered_sources` | 0.26.0 |
| `registered_source` | alias of `lispy_hosted.registered_source` | 0.26.0 |
| `run_registered_governor` | alias of `lispy_hosted.run_registered_governor` | 0.26.0 |
| `_artifact_sha256` | alias of `lispy_hosted._artifact_sha256` | 0.26.0 |
| `_worker_manifest` | alias of `lispy_hosted._worker_manifest` | 0.26.0 |
| `_handle_worker_request` | alias of `lispy_hosted._handle_worker_request` | 0.26.0 |
| `_json_line` | alias of `lispy_hosted._json_line` | 0.26.0 |
| `_worker_environment` | alias of `lispy_hosted._worker_environment` | 0.26.0 |
| `_run_worker_once` | alias of `lispy_hosted._run_worker_once` | 0.26.0 |
| `_pump_worker_pipe` | alias of `lispy_hosted._pump_worker_pipe` | 0.26.0 |
| `_feed_worker_stdin` | alias of `lispy_hosted._feed_worker_stdin` | 0.26.0 |
| `_kill_worker` | alias of `lispy_hosted._kill_worker` | 0.26.0 |
| `_supervise_worker` | alias of `lispy_hosted._supervise_worker` | 0.26.0 |
| `_run_jsonl_supervisor` | alias of `lispy_hosted._run_jsonl_supervisor` | 0.26.0 |
| `SAMPLE_STATE_FILES` | alias of `lispy_hosted.SAMPLE_STATE_FILES` | 0.26.0 |
| `_sample_state_sha256` | alias of `lispy_hosted._sample_state_sha256` | 0.26.0 |
| `_doctor_worker_request` | alias of `lispy_hosted._doctor_worker_request` | 0.26.0 |
| `_canonical_sha256` | alias of `lispy_hosted._canonical_sha256` | 0.26.0 |
| `_normalize_sha256_pin` | alias of `lispy_hosted._normalize_sha256_pin` | 0.26.0 |
| `_verify_worker_response_digests` | alias of `lispy_hosted._verify_worker_response_digests` | 0.26.0 |
| `_doctor_request` | alias of `lispy_hosted._doctor_request` | 0.26.0 |
| `_doctor_report` | alias of `lispy_hosted._doctor_report` | 0.26.0 |
| `_print_doctor_report` | alias of `lispy_hosted._print_doctor_report` | 0.26.0 |
| `_collect_replay_evidence` | alias of `lispy_hosted._collect_replay_evidence` | 0.26.0 |
| `_doctor_check` | alias of `lispy_hosted._doctor_check` | 0.26.0 |
| `INSTALLED_REQUIRED_PATHS` | alias of `lispy_hosted.INSTALLED_REQUIRED_PATHS` | 0.26.0 |
| `BUILD_INPUT_PATHS` | alias of `lispy_hosted.BUILD_INPUT_PATHS` | 0.26.0 |
| `_verify_distribution_record` | alias of `lispy_hosted._verify_distribution_record` | 0.26.0 |
| `_verify_record_content` | alias of `lispy_hosted._verify_record_content` | 0.26.0 |
| `_validate_distribution_record_paths` | alias of `lispy_hosted._validate_distribution_record_paths` | 0.26.0 |
| `_installed_doctor_profile` | alias of `lispy_hosted._installed_doctor_profile` | 0.26.0 |
| `_effects_doctor_profile` | alias of `lispy_hosted._effects_doctor_profile` | 0.26.0 |
| `_doctor_v2` | alias of `lispy_hosted._doctor_v2` | 0.26.0 |
| `_source_inventory` | alias of `lispy_hosted._source_inventory` | 0.26.0 |
| `build_source_sha256` | alias of `lispy_hosted.build_source_sha256` | 0.26.0 |
| `_inventory_payload` | alias of `lispy_hosted._inventory_payload` | 0.26.0 |
| `_v3_component` | alias of `lispy_hosted._v3_component` | 0.26.0 |
| `_v3_failed_component` | alias of `lispy_hosted._v3_failed_component` | 0.26.0 |
| `DOCTOR_V3_CHECK_CATALOGS` | alias of `lispy_hosted.DOCTOR_V3_CHECK_CATALOGS` | 0.26.0 |
| `_validate_doctor_v3_error` | alias of `lispy_hosted._validate_doctor_v3_error` | 0.26.0 |
| `_validate_doctor_v3_sha256` | alias of `lispy_hosted._validate_doctor_v3_sha256` | 0.26.0 |
| `_validate_doctor_v3_report` | alias of `lispy_hosted._validate_doctor_v3_report` | 0.26.0 |
| `_doctor_v3` | alias of `lispy_hosted._doctor_v3` | 0.26.0 |
| `_run_doctor` | alias of `lispy_hosted._run_doctor` | 0.26.0 |
| `_run_replay` | alias of `lispy_hosted._run_replay` | 0.26.0 |
| `_reject_present_options` | alias of `lispy_hosted._reject_present_options` | 0.26.0 |
| `base64` | alias of `lispy_hosted.base64` | 0.26.0 |
| `csv` | alias of `lispy_hosted.csv` | 0.26.0 |
| `importlib` | alias of `lispy_hosted.importlib` | 0.26.0 |
| `signal` | alias of `lispy_hosted.signal` | 0.26.0 |
| `subprocess` | alias of `lispy_hosted.subprocess` | 0.26.0 |
| `tempfile` | alias of `lispy_hosted.tempfile` | 0.26.0 |
| `threading` | alias of `lispy_hosted.threading` | 0.26.0 |
| `time` | alias of `lispy_hosted.time` | 0.26.0 |
| `VERSION` (release file) | current; mirrors `lisp.VERSION` | 0.26.0 |
| `_HOSTED_NAMES` | current; lazy compatibility export registry | 0.26.0 |
| `_hosted_binding` | current; deferred host binding | 0.26.0 |
| `__getattr__` | current; lazy module forwarding | 0.26.0 |
| `__dir__` | current; discoverable compatibility exports | 0.26.0 |
| `__all__` | current; preserves wildcard imports | 0.26.0 |
