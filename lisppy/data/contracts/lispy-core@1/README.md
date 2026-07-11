# LisPy Core 1

`lispy-core@1` is the portable contract implemented by `lisp.py`.

- Evaluation is left-to-right.
- Only `#f` and `nil` are false.
- `#t`/`#f` are canonical booleans; `true`/`false` are compatibility aliases.
- The portable `stdlib.lisp` contract contains only `identity`, `constantly`,
  `complement`, and `partial`.
- The default Python runtime layers optional Rappterbook read/plan helpers for
  source compatibility and reports those profiles separately.
- Host capabilities are separate from core semantics and denied unless enabled.
- `do`, quasiquote, browser APIs, and Mars host actions are not part of Core 1.
- Deterministic execution limits are an implementation safety profile, not a
  change to Core 1 value semantics.

`conformance.json` uses `lispy-conformance@2` and the language-neutral
`lispy-value@1` wire documented in `CONFORMANCE.md`. Run it with:

```bash
python3 -m unittest tests.test_conformance
```

Another runtime may claim `lispy-core@1` compatibility only after passing the same cases.
