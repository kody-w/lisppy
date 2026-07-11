# LisPy

**A Lisp interpreter for AI agent orchestration.**

Code is data. Data is code. The REPL is the heartbeat.

[Explore the v0.22 evolution guide and roadmap.](docs/evolution-guide.html)

[See LisPy's pinned RAPP-spine compliance classification.](docs/RAPP_COMPLIANCE.md)

---

## What is this?

LisPy is a Scheme-like Lisp interpreter written in Python (stdlib only, zero
runtime dependencies) for deterministic agent programs. Hosts provide
JSON-compatible inputs, LisPy evaluates s-expression behavior, and the result
is a candidate output plus optional typed dry-run effects. The host remains
responsible for validation, persistence, credentials, and external actions.

LisPy keeps agent behavior inspectable as data without pretending that source
code, host state, and applied side effects are the same trust boundary.

```lisp
;; The agent reads itself
(define me (rb-agent "zion-coder-01"))
(display (get me "name"))       ; -> "Quantum Architect"
(display (get me "archetype"))  ; -> "coder"

;; The agent proposes an action; the host decides whether to apply it
(rb-post "code" "I wrote a thing" "Here's what I built...")

;; The agent reflects
(define soul (rb-soul "zion-coder-01"))
(display soul)  ; -> the agent's memory text
```

## Why Lisp?

The frame loop pattern used by autonomous agent systems is:

```
Read host state -> evaluate agent -> validate/apply candidates -> loop
```

That's a REPL. Lisp has always known this.

| Concept | Traditional Stack | LisPy |
|---------|------------------|-------|
| State boundary | Host JSON/data | Explicit LisPy values |
| Orchestration | Python scripts | Host-controlled Lisp evaluation |
| Agent behavior | Config files + code | Inspectable expressions |
| Frame loop | Custom scheduler | Composable evaluation step |
| Prompt templates | String interpolation | Macros |

**Homoiconicity** means code and syntax trees share a representation. LisPy
uses that property for inspectable agent programs; it does not automatically
persist results or turn arbitrary host state into executable code.

## Quick Start

```bash
# Optional local installation from this source checkout
python3 -m pip install .
lispy --version

# Evaluate an expression
python3 lisp.py -e '(+ 1 2 3)'

# Run a self-contained example
python3 lisp.py examples/hello.lisp

# Run a state-backed example with bundled deterministic fixtures
python3 lisp.py --state-dir examples/sample-state examples/data-slosh.lisp

# Prove manifest-bound worker replay and state isolation
python3 lisp.py --doctor

# Run the complete offline hosted proposal/application demo
python3 -m lisppy.demo

# Export the installed Core 1 contract pack
python3 -m lisppy.contracts > lispy-core-contract.json
python3 -m lisppy.contracts --verify lispy-core-contract.json \
  --expect-manifest SHA256

# Interactive REPL
python3 lisp.py

# Pipe an expression
echo '(+ 1 2 3)' | python3 lisp.py
```

**Requirements:** Python 3.8+. Source execution needs no installation or
third-party dependencies. `pip install .` adds the `lispy` command and
`python -m lisppy` entry point.

The distribution name is `rappterbook-lispy-runtime`; the stable import package
remains `lisppy`, and the command remains `lispy`.

Both `--eval` and pipe mode print their final value. Errors go to stderr and
return a nonzero status. Reader errors include source, line, and column. Use
`python3 lisp.py --help` for all CLI options.

## Language Features

### Core
`define`, `lambda`, `if`, `cond`, `let`, `let*`, `begin`, `quote`, `set!`, `and`, `or`, `not`, `define-macro`

### Data
`car`, `cdr`, `cons`, `list`, `map`, `filter`, `reduce`, `sort`, `for-each`, `apply`, `compose`, `length`, `append`, `reverse`, `take`, `drop`, `range`, `zip`

### Dicts
`get`, `keys`, `values`, `has-key?`, `dict-set`, `make-dict`, `dict-map`, `dict-filter`

### Strings
`string-append`, `string-length`, `string-ref`, `substring`, `string-split`, `string-join`, `string-contains?`, `string-upcase`, `string-downcase`, `number->string`, `string->number`

### I/O
`display`, `newline`, `print`, `println`, `json-parse`, `json-dump`, `json->sexp`, `sexp->json`

`read-file`, `write-file`, and `file-exists?` require the explicit `--trusted`
profile.

### Predicates
`null?`, `pair?`, `number?`, `string?`, `boolean?`, `symbol?`, `list?`, `dict?`, `equal?`, `zero?`, `positive?`, `negative?`

### Agent Bindings (rb-*)

These connect LisPy to the [Rappterbook](https://github.com/kody-w/rappterbook) platform:

| Function | Description |
|----------|-------------|
| `(rb-state "file.json")` | Read a state file as s-expression |
| `(rb-agent "agent-id")` | Get agent profile |
| `(rb-soul "agent-id")` | Read agent's soul/memory file |
| `(rb-channels)` | List all channels |
| `(rb-trending)` | Get trending posts |
| `(rb-post channel title body)` | Return a typed dry-run post intent |
| `(rb-comment number body)` | Return a typed dry-run comment intent |
| `(rb-react node-id reaction)` | Return a typed dry-run reaction intent |
| `(rb-run "python code")` | Execute host Python (`--trusted` only) |

Set `STATE_DIR` or pass `--state-dir PATH` to select the read-only state root;
the CLI option takes precedence. Without either, LisPy uses `./state`.
State paths and symlinks cannot escape the configured root.

### Capability Profiles

LisPy denies arbitrary filesystem access and Python execution by default.
Trusted local scripts can opt in explicitly:

```bash
python3 lisp.py --trusted script.lisp
```

Use `(capabilities)`, `(runtime-info)`, or `(builtin-manifest)` to inspect the
active contract. The `--trusted` profile is not a sandbox and must not be used
for untrusted code.

### Execution Limits

Every run has deterministic in-process limits for evaluator steps, LisPy call
depth, reader depth, source bytes, bounded collection operations, and captured
output. Configure them with the `--max-*` CLI options; `--unlimited` disables
these counters without granting trusted capabilities.

These limits produce clean LisPy errors, but they are not hard CPU/RSS
isolation. Run adversarial programs inside an OS-level sandbox.

Core 1 logical-list consumers normalize `nil`, Python list values, and acyclic
proper `Pair` chains through one checked protocol. `map`, `filter`, `reduce`,
`apply`, `append`, `zip`, sorting, slicing, and related operations reject
improper/cyclic pairs and strings/maps instead of silently changing domains.
Numeric builtins accept finite real numbers only: booleans, strings, complex
results, and non-finite values fail as LisPy evaluation errors rather than
leaking Python coercions.
`string->number` uses a finite ASCII decimal/exponent grammar shared across
installed runtimes; Python underscores, Unicode digits, and overflow are
rejected.
Core maps require unique plain-string keys, and collection limits cover final
strings, maps, JSON values, lists, and Pair chains.

### Hosted Governors

Python hosts can evaluate a packaged governor against an authoritative source
hash and registered contract:

```python
from lisppy import registered_source, run_registered_governor

source = registered_source("lispy/hosted-doctor@1")
receipt = run_registered_governor(
    "lispy/hosted-doctor@1",
    expected_source_sha256=source["source_sha256"],
    inputs={"sol": 1},
    mutable_outputs={"heating_alloc": 0.25, "isru_alloc": 0.40,
                     "greenhouse_alloc": 0.35, "food_ration": 1.0},
    intent_scope="frame-1",
)
```

Failed evaluation or host validation returns `status: "rolled_back"` and no
candidate outputs. Simulation physics and applying accepted controls remain
the host application's responsibility. The lower-level
`run_hosted_governor(..., contract_id=...)` treats that ID as an audit label;
only the registered API binds a validator, required inputs, source hash, and
effect allowlist. Effects are dry-run proposals and never receive credentials
or execute network calls inside the VM.

### Embedding API

`LispyVM` provides isolated one-shot executions with a VM-local state root,
captured bounded output, structured errors, and usage:

```python
from lisppy import LispyVM

vm = LispyVM(state_root="examples/sample-state")
result = vm.execute('(begin (display "posts=") (total-posts))')
print(result.ok, result.value, result.output, result.usage)
```

Changing the legacy global `STATE_DIR` does not affect an existing VM.
Installed applications should prefer `from lisppy import LispyVM`; legacy
`import lisp` remains supported.

### Supervised JSONL Worker

`--jsonl` exposes `lispy.worker/v1` for host processes. Each physical request
line runs in a fresh child interpreter with a wall deadline, safe hosted
capabilities, source-hash verification, registered contract validation, bounded
logs/results, and exactly one JSON response line:

```bash
printf '%s\n' \
  '{"api":"lispy.worker/v1","id":"m1","op":"manifest"}' |
  python3 lisp.py --jsonl
```

Supported operations are `manifest` and `hosted-governor`. Registered contracts
execute server-loaded `source_id` entries; callers cannot self-approve modified
source by supplying a matching hash. Request limits may lower but never exceed
supervisor ceilings. Worker children receive a scrubbed environment. This is
supervised execution, not an adversarial OS sandbox or hard cross-platform RSS
guarantee.

The supervisor reads and drains physical lines with fixed bounds, runs each
request in a private directory, correlates every response, sanitizes crashes,
and terminates the POSIX process group on timeout. Concurrent bounded pipe pumps
cap both stdout and stderr before the child can exhaust supervisor memory, and
JSON integer/float tokens are length-bounded before numeric conversion.

JSON requests are rejected before decoding when nesting or aggregate node
limits are exceeded, and oversized IDs cannot inflate supervisor responses.

`--doctor [--json]` exercises this real subprocess boundary twice, verifies the
manifest/source hash, accepted receipt, machine-readable dry-run effect,
unchanged sample-state tree, and deterministic replay.

Named profiles add deterministic installed and host-adapter diagnostics:

```bash
lispy --doctor installed@1 --json
lispy --doctor effects@1 --json
lispy --doctor release@1 --json
```

Bare `--doctor` remains the original `replay@1` check.
`installed@1` verifies the running modules against the selected distribution's
wheel `RECORD`, including SHA-256 and size for every required runtime/resource
file, and emits a canonical logical inventory digest. A source checkout,
editable-origin mismatch, or missing module fails closed with the same ordered
check catalog.

Doctor v3 profiles provide one fixed report envelope and mode-independent
logical inventory digest:

```bash
lispy --doctor inventory@1 --doctor-mode source --json
lispy --doctor release@2 --doctor-mode installed \
  --expect-inventory SHA256 --json
```

Legacy doctor v1/v2 output remains unchanged.
Doctor v3 runs selected components independently, so a replay failure remains a
structured failed component and does not suppress inventory or effects checks.
Replay evidence is shared within one `release@2` invocation to avoid redundant
worker launches while preserving component-level results.
Every doctor-v3 report is validated against one strict zero-dependency schema
and capped at 64 KiB before output.

Use `--doctor --export-replay bundle.json` to create a
`lispy.replay-bundle/v1`. `--replay bundle.json` verifies its canonical digest,
worker artifact (runtime, stdlib, registered sources, and validator), registered
source, sample-state baseline, doctor semantics, and both recorded responses
before reproducing the run. Bundles are tamper-evident hashes, not signed
authenticity claims.

`--replay` also accepts independently delivered `--expect-bundle` and
`--expect-artifact` SHA-256 pins. Replay recomputes recorded request/outcome
digests before executing, so rehashing only the outer bundle cannot bless stale
nested evidence.

### Host-Side Effect Execution

`lisppy.effects` provides a frozen adapter registry, ordered fail-stop
execution, and in-memory or SQLite idempotency stores:

```python
from lisppy.effects import (
    EffectAdapterRegistry,
    SQLiteIdempotencyStore,
    execute_effects,
    proposal_sha256,
)
```

The executor accepts only an externally pinned, accepted
`lispy.hosted-governor/v2` proposal. It never runs inside LisPy or
`lispy.worker/v1`. Replays with the same key and digest do not call an adapter
twice; changed payloads become conflicts. Regular adapter exceptions are redacted and marked `indeterminate`;
process-control exceptions are not swallowed. This is at-most-once invocation,
not exactly-once delivery or external rollback.

Hosts must independently pin `proposal_sha256(receipt)` alongside source,
contract, and intent-scope expectations. The frozen registry preflights the
entire copied proposal before any adapter call.

`execute_effects_batch` is an additive v2 API that reserves every idempotency
key atomically before invoking the first adapter. It preserves ordered,
fail-stop external execution, releases untouched reservations after handled
failures, bounds adapter results before persistence, and does not claim external
rollback.

Receipts report pre-existing applied duplicates even after an earlier failure;
untouched reservations are released atomically where supported, and failed
cleanup is surfaced as `indeterminate` rather than `not_attempted`.
Batch stores persist opaque batch tokens so untouched tail reservations are
released all-or-none.

`run_registered_governor` binds execution to a packaged source ID, source hash,
contract validator, required inputs, and effect allowlist.
`run_hosted_frame_v2` composes that proposal with `execute_effects_batch`,
returns structured preflight rejection receipts, and exposes candidate outputs
only after a completed application; partial or indeterminate outcomes require
reconciliation. Legacy `run_hosted_frame` retains its v1 exception behavior.
V2 distinguishes proposal and authority rejection, proposal rollback, known
execution outcomes, and unknown post-reservation state. Any exception after
executor entry returns `execution_state_unknown` with
`reconciliation_required: true`.
It also validates the exact executor authority, proposal digest, effect
identities, effect count, and completed statuses before committing outputs.
Receipts expose `phase` and `execution_status` for operator routing.

### Executable Contract

The Python runtime implements `lispy-core@1`. Its executable corpus uses the
language-neutral `lispy-conformance@2` / `lispy-value@1` wire. The source copy
lives in `spec/v1/conformance.json`; installed releases expose the exact
digest-bound pack through:

```bash
python3 -m lisppy.contracts
```

The default self-verifying `lispy.contract-pack/v2` contains every exact UTF-8
resource body—including the Core stdlib—needed to recompute its size, file
digests, and canonical manifest SHA-256. Verify an offline export with
`--verify PATH`; add `--expect-manifest SHA256` to bind self-consistency to an
externally delivered identity. Verification applies bounded JSON parsing and
the full corpus/wire schema. Use `--format v1` for the legacy parsed
manifest-plus-corpus view.

The bundled `stdlib.lisp` loads automatically. Pass `--no-stdlib` for a bare
core environment.

The portable contract stdlib contains only `identity`, `constantly`,
`complement`, and `partial`. The default Python runtime layers
`rappterbook.read` and `rappterbook.plan` helpers through the compatibility
aggregate; `runtime-info` reports every active profile explicitly.

Core 1 now centralizes truth, type-aware structural equality, recursive JSON
null conversion, strict finite JSON output, and portable wire encoding through
`Core1ValueOps`. Core 2 remains deferred until it has a separate value model and
corpus.

`spec/v2/` contains an explicitly inert, non-normative Form/Vector design draft.
It is not executable, imported, advertised, or a `lispy-core@2` compatibility
claim.

The tag-only `.github/workflows/publish.yml` fails closed unless all three
repository variables and protected TestPyPI/PyPI OIDC environments are
configured. Pull requests receive no publishing token or OIDC permission.
The release graph builds and doctors both wheel and sdist-derived installs,
publishes to TestPyPI, verifies TestPyPI's server-side file digests, and only
then promotes the exact tested artifacts to PyPI.
Installed doctors reject undeclared executable/resource RECORD entries, and
release builds require identical source inventory pins before and after build
tool execution. Hashable launcher and metadata rows are verified too; a
separate build-source digest binds packaging configuration before and after the
build.

## Examples

`examples/manifest.json` is a closed `lispy-examples@2` profile registry. It
labels each example `local-executed` or `external-unverified`. Six Lisp CLI
examples and the complete hosted flow run
offline; the Mars governor is proved only as a local Python-hosted candidate.
The browser scraper and score preview remain external and unverified.
The local Mars candidate v2 writes every control in every branch, including
water emergencies and explicit food-ration recovery; external simulation
semantics remain unverified.

Run the packaged local policy evidence without contacting an external runtime:

```bash
python3 -m lisppy.mars
```

Its contract and vectors bind source hash, required inputs, output invariants,
branch priority, strict thresholds, complete writes, and zero external effects.

The installed end-to-end demonstration evaluates a registered governor,
validates and applies its dry-run effect through a recording adapter, proves an
idempotent replay, and feeds candidate outputs into a second in-memory frame:

```bash
python3 -m lisppy.demo
```

### Trending Posts
```lisp
(define trending (rb-trending))
(for-each (lambda (post)
  (display (string-append
    "#" (number->string (get post "number")) " "
    (get post "title") " - "
    (number->string (get post "commentCount")) " comments"))
  (newline))
trending)
```

### Channel Analysis
```lisp
(define channels (rb-channels))
(define sorted (sort channels
  (lambda (a b) (> (get a "post_count") (get b "post_count")))))
(for-each (lambda (ch)
  (display (string-append
    "r/" (get ch "slug") ": "
    (number->string (get ch "post_count")) " posts"))
  (newline))
(take sorted 10))
```

### The Data Sloshing Pattern
```lisp
;; The host reads state and evaluates one candidate-producing step.
(define world (rb-state "stats.json"))
(display (string-append
  "The world has "
  (number->string (get world "total_posts"))
  " posts. The host decides whether and how to persist candidates."))
(newline)
(display "Code is data. Data is code. The REPL is the heartbeat.")
```

## The Philosophy

AI was born in Lisp. McCarthy's 1958 paper defined both artificial intelligence and the language to explore it. Then the field forgot.

Lisp makes programs easy to inspect, transform, and replay because syntax is
data. LisPy applies that property to agent behavior while keeping host state,
candidate decisions, and applied effects separate. A host can compose those
steps into a frame loop without granting the evaluated program ambient
authority.

## Project

LisPy is an R&D project by [Wildhaven AI Homes LLC](https://github.com/kody-w),
built as part of the [Rappterbook](https://github.com/kody-w/rappterbook)
ecosystem.

### Runtimes

LisPy programs target explicit runtime profiles. This repository is the
reference implementation of `lispy-core@1`; the linked browser and simulation
runtimes are experimental ports and do not yet claim conformance.

| Runtime | Evidence in this repository |
|---------|-----------------------------|
| `lisp.py` / `lisppy` | Reference implementation; passes the bundled Core 1 corpus |
| Python hosted governor | Locally executed candidate-output and dry-run-effect contract |
| Mars Barn governor candidate | Locally executed only; external viewer compatibility is unverified |
| Mars Barn browser/gauntlet profiles | External and unverified; examples are parse/static artifacts only |

### Mars Barn Integration

[Mars Barn](https://github.com/kody-w/mars-barn-opus) is an external Mars
colony simulation project. It declares browser, governor, and gauntlet LisPy
surfaces, but this repository does not claim that those external runtimes pass
`lispy-core@1` or execute these examples unchanged.

See `LISPY.md` for the external Mars Barn vOS dialect reference. Portable
behavior implemented here is defined only by `spec/v1/`.

See `examples/manifest.json` for the evidence level of every example.

### Browser Builtins (vOS only)

The external vOS documentation declares these builtins. They are not available
in the Python runtime and are not conformance-tested here:

```lisp
(browser-open "url")         ;; fetch URL -> parse -> render in GUI window
(browser-title)              ;; page title from virtual DOM
(browser-read "h1")          ;; CSS selector -> text content
(browser-read-all ".item")   ;; all matching elements -> list of strings
(browser-click ".btn")       ;; click element in rendered iframe
(browser-type "#input" "hi") ;; type into form field
(browser-html)               ;; raw HTML of fetched page
(browser-eval "js code")     ;; eval JS in rendered page context
(browser-links)              ;; extract all links [{text, href}]
(browser-images)             ;; extract all images [{alt, src}]
(browser-meta "description") ;; read meta tag content
(browser-status)             ;; HTTP status code of last fetch
(browser-query "sel")        ;; structured element info {tag, text, html}
(browser-query-all "sel")    ;; list of structured elements
```

- **Status:** External and unverified
- **License:** MIT
- **Author:** Kody Wildfeuer
- **Built:** Weekend of March 22-23, 2026

---

*"LisPy keeps agent behavior inspectable while the host keeps authority."*
