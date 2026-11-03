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
