# Building with your AI (the LisPy pattern)

Ruling (Kody Wildfeuer, 2026-11-03): the way LisPy itself was locked in is the pattern for anyone building
anything in LisPy with their AI. Seven steps; none of them is optional.

1. **Say the taste in plain words.** The owner states what good looks like, never how to build it.
2. **The AI writes the oracle first.** Before any code: the expected inputs and outputs (`expected.json`, a
   conformance corpus). The oracle is readable by the owner; the AI never builds against prose.
3. **Build blind against the oracle.** The program is written to pass the corpus. If an AI with no history can
   derive it from the oracle plus `llms.txt`, it is right; if not, the oracle is incomplete.
4. **The owner touches the live thing.** Not the report. Run it, call it, read what it said.
5. **Every new noun gets a status before it is used.** current · staging · alias of X · retiring, in `VOCABULARY.md`.
   No new form, file type or name without a line there.
6. **Every lesson paid for is written down** so the next AI starts ahead (`FREE-TUITION.md`).
7. **A stranger can continue.** Any `.lispy` file carries `;; lispy-core@1 — https://kody-w.github.io/lisppy/llms.txt`
   on line 1. An AI holding only the file and that page needs nothing else.

The reference example is `examples/brainstem-kernel/`: a complete RAPP Brainstem written this way, proven against
the rapp-1 oracle (25/25) by an AI that had never seen the original source.
