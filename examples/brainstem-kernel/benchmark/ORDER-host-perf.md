# ORDER — LisPy Brainstem host: perf + correctness (Phase 2)

## Intent
`lispy_host.py` runs `brainstem.lispy` (LisPy, lispy-core@1) as a RAPP Brainstem. It already passes the rapp-1 grail
oracle 25/25. Make it faster where a person waits and fix two correctness gaps, WITHOUT changing the oracle result
and WITHOUT adding a dependency (stdlib only, forever). The kernel owns logic; the host owns I/O.

## Inputs to study
- `lispy_host.py`, `brainstem.lispy`, `soul.md` (this directory). `lisppy/` is a symlink to the pinned interpreter;
  never edit anything under it. `LISPPY_SHA256` must stay as is.
- The oracle: `~/Wildhaven/brainstem-distro-lab/rapp-1/conformance/grail/run.py` (+ `scenarios.py`, `expected.json`,
  `fake_model.py`). Read how `cmd:` candidates are started and which env vars they get.
- The grail for reference behaviour only (read, never copy): `~/Wildhaven/brainstem-distro-lab/grail-pin-0616/rapp_brainstem/brainstem.py`
  (its `_register_shims`, `/chat/stream` SSE shape, `load_agents`).

## Build (all in `lispy_host.py` unless stated; keep it one file)
1. **Agent reload by directory change.** Keep a cache keyed by (AGENTS_PATH, max mtime of `*_agent.py` + dir mtime);
   reload only when it changes. "Edit an agent and the next request sees it" must still hold (test it).
2. **`utils` shim.** Register `sys.modules["utils"]`, `utils.azure_file_storage` → the grail's `local_storage.py`
   semantics (copy the minimal shim shape from `_register_shims`; stdlib only). Today `context_memory_agent.py` and
   `manage_memory_agent.py` are skipped with "No module named 'utils'"; after this they load.
3. **Parse once.** Evaluate `KERNEL_SOURCE` once per kernel identity (sha256 of the file bytes is fine here) into a
   base env; per request, create a child env (or re-run only the cheap binding step) so the kernel is not re-parsed
   per request. Measure before/after.
4. **`POST /chat/stream`.** Server-sent events in the grail's shape (read it from brainstem.py; the final event
   carries the same JSON `/chat` would return). The kernel stays pure: add a host binding the kernel can call to emit
   a chunk (e.g. `emit`), and a `handle-chat-stream` in `brainstem.lispy` that uses it. `/chat` must be unchanged.
5. **Parallel tool calls in a round.** When one model reply holds N tool calls, run them concurrently
   (`concurrent.futures.ThreadPoolExecutor`, stdlib) via a host binding `agents-perform-all` that preserves order;
   the kernel's `run-tool-calls` uses it. Results must be identical to sequential (the oracle will tell you).
6. **ensurepip fallback.** When an agent import fails with ModuleNotFoundError, try `python3 -m ensurepip` once then
   `pip install <module>` like the grail does; log one line; never loop.
7. **`bench.py`** (new file, this directory): runs the 25 oracle scenarios against BOTH `cmd:python3 lispy_host.py`
   and the grail (`grail:` mode) with the same fake model, prints a table (scenario, lispy ms, grail ms), plus
   boot-to-serve ms, RSS MB, and `/health` median over 30 requests. Exit 1 if LisPy is slower than the grail on any
   scenario by more than 25 % (CPU-bound scenarios excluded: there are none today).

## Rules
- Only edit: `lispy_host.py`, `brainstem.lispy`, new `bench.py`, new `tests_host.py`. Nothing else. Do not touch
  `lisppy/`, the oracle, or the grail.
- Stdlib only. No new flags or settings; env vars already defined are the only configuration.
- Every change ships with a check in `tests_host.py` (unittest).

## Acceptance (run these, paste verbatim output)
1. `cd ~/Wildhaven/brainstem-distro-lab/rapp-1/conformance/grail && GRAIL_DIR=~/Wildhaven/brainstem-distro-lab/grail-pin-0616/rapp_brainstem AGENTS_PATH=~/Wildhaven/brainstem-distro-lab/lispy-brainstem/agents python3 run.py --grail "$GRAIL_DIR" --candidate "cmd:python3 $HOME/Wildhaven/brainstem-distro-lab/lispy-brainstem/lispy_host.py"` → `25 of 25 scenarios identical`
2. `python3 tests_host.py` → all pass (includes: reload-on-edit, utils shim loads the two memory agents, stream
   final event equals /chat body, parallel results equal sequential).
3. `python3 bench.py` → the table, exit 0.
4. `AGENTS_PATH=~/.brainstem/src/rapp_brainstem/agents PORT=7098 python3 lispy_host.py` then
   `curl -s localhost:7098/health` lists the memory agents (then stop it: `# owner-checked: 7098 is the bench host`).

## Done-when + report
All four acceptance outputs pasted verbatim; a "flags / surprises" section (flag uncertainty, never paper over);
before/after numbers for 1, 3, 5. Resume id at the end.
