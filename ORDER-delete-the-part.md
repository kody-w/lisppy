# ORDER: delete-the-part on lisp.py (Musk's algorithm step 3, applied to a language)

## Intent
`lisp.py` is 6,032 lines. The LANGUAGE (tokenize, parse, Env, evaluate, make_global_env, Core1ValueOps,
LispyVM, to_wire, repl, identity/format/teach, the CLI) is roughly the first 2,750 lines. The remaining
~3,300 lines are HOSTING machinery: hosted governor, registered governor, jsonl supervisor, worker
requests, doctor v3, distribution records, replay, receipts. The bar (Kody): "beautiful, made by an advanced
alien race": one file is the language, nothing arbitrary, and a reader of llms.txt finds the interpreter they
were promised, not a hosting platform. Move the hosting machinery OUT of lisp.py into one sibling module,
`lispy_hosted.py`, WITHOUT breaking a single existing command, test, flag, hash or published path
(Linux playbook: never break userspace; delete by aliasing, never by breaking).

Target: `lisp.py` <= 2,500 lines, all 185+ tests green, every CLI mode works exactly as before.

## Inputs to study (all inside this directory)
- `lisp.py` (the whole file; map its sections before cutting)
- `tests/` (every test; they are the net — run them first to see 185 pass)
- `VOCABULARY.md` (the admission rule and statuses: current / staging / alias of X / retiring)
- `MAINTAINERS`, `CHANGELOG.md`, `rapp-compliance.json`, `llms.txt`, `lisppy/data/llms.txt`
- `lisppy/` package dir (packaged copies of data files; `INSTALLED_REQUIRED_PATHS` in lisp.py lists them)
- `examples/brainstem-kernel/lispy_host.py` (pins sha256 of lisp.py — see rule 6)
- `spec/mars/` and `lisppy/data/mars` governor hashes (see rule 6)
- `BUILDING-WITH-YOUR-AI.md` (how this repo is built; follow it)

## Rules / do-not-touch
1. Do NOT change language semantics, the stdlib, `llms.txt` content, `--identity`, `--format`, `--teach`,
   the magic line, error messages, or any test's expected output. Tests may be MOVED to match the new module
   but their assertions stay byte-identical. Add tests only for the alias/forwarding surface.
2. Every flag `lisp.py` accepts today keeps working from `python3 lisp.py` (e.g. `--doctor`, `--replay`,
   governor/supervisor/worker modes): `lisp.py` imports `lispy_hosted` lazily, only when such a mode is asked
   for. A plain `python3 lisp.py file.lispy` must NOT import `lispy_hosted` at all (prove it: run with
   `python3 -X importtime` or `-v` and grep).
3. `lispy_hosted.py` is ONE file beside `lisp.py` and gets a packaged copy wherever the package needs it
   (follow how `lisp.py` is packaged in `lisppy/`; update `INSTALLED_REQUIRED_PATHS`, `MANIFEST.in`,
   `pyproject.toml`/`setup.cfg` if they list files). `pip install .` in a temp venv must still work and
   `python -m lisppy --doctor` (or whatever the packaged entry is) must still run.
4. Public names that moved get a forwarding alias in `lisp.py` (`from lispy_hosted import X` on demand, or a
   `__getattr__` module hook) so any external `import lisp; lisp.run_hosted_governor` keeps working. Record
   each moved name in `VOCABULARY.md` under a new row: name, status `alias of lispy_hosted.X`, since 0.26.0.
5. No new dependencies. No pip. Python 3.11 compatible.
6. HASH PINS: `examples/brainstem-kernel/lispy_host.py` pins the sha256 of `lisp.py` (search `2a6be927`);
   spec/mars and lisppy/data/mars pin governor source hashes; tests check them. Re-pin ONCE at the end, in a
   single commit titled `re-pin lisp.py sha after delete-the-part`, and print the new sha256 in your report.
   Do NOT touch `~/Wildhaven/brainstem-distro-lab/lispy-brainstem/` or any rapp-1 file (outside your cwd;
   Fable re-pins those).
7. `VERSION`/`lisp.py` VERSION -> 0.26.0; CHANGELOG entry "0.26.0 — delete-the-part: the hosting
   machinery moved to lispy_hosted.py; lisp.py is the language (N lines)"; `rapp-compliance.json` version bump
   the same way 0.25.0 was done.
8. Only create/modify files inside this repo. Work on branch `delete-the-part` (already checked out).
   Commit as you go with clear messages. Do NOT push.
9. Flag uncertainty, do not paper over it. If a cut would change behaviour, keep the code in lisp.py and
   say why in the report.

## Numbered acceptance checks (run verbatim; paste the output in the report)
1. `wc -l lisp.py lispy_hosted.py` -> lisp.py <= 2500
2. `python3 -m pytest tests -q 2>&1 | tail -3` -> all pass, count >= 185
3. `python3 lisp.py --identity examples/hello.lispy` (or any example) -> prints `sha256:...` unchanged from
   `git stash`-free baseline: capture the SAME command's output on `main` first (`git show main:lisp.py >
   /tmp/lisp_main.py; python3 /tmp/lisp_main.py --identity <same file>`) and show both are equal.
4. `python3 -c "import sys; sys.argv=['lisp.py','examples/hello.lispy']; import lisp" ; python3 -X importtime lisp.py examples/hello.lispy 2>&1 | grep -c lispy_hosted` -> 0
5. `python3 lisp.py --doctor 2>&1 | tail -5` (and every other hosted mode listed in `_argument_parser`) -> same
   exit code and same first/last lines as on main (show both).
6. `python3 lisp.py --teach | sha256sum` equals `sha256sum llms.txt` (teach output unchanged)
7. Fresh venv: `python3 -m venv /tmp/lv && /tmp/lv/bin/pip install -q . && /tmp/lv/bin/python -c "import lisppy, lisp" ` (adapt to the actual package layout) -> ok
8. `git diff main --stat | tail -1` and `git log --oneline main..HEAD`

## Done-when + report
Done when checks 1-8 pass. End with a report containing: the section map you cut by (line ranges moved),
the VOCABULARY rows added, the new sha256 of lisp.py, the verbatim outputs of checks 1-8, and a
"Flags / surprises" section (anything you were unsure about, anything that would have changed behaviour and
therefore stayed).
