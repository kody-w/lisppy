# ORDER — blind derivation: write a RAPP Brainstem kernel in LisPy from the ORACLE ALONE

## Intent
Spinning-top sanity check. You must write `brainstem.lisp`, a RAPP Brainstem kernel in LisPy (kody-w/lisppy,
profile lispy-core@1), using ONLY: the oracle in ./oracle (scenarios.py = the 25 requests, expected.json = the
exact answers the pinned Python kernel gives, fake_model.py = the deterministic model stand-in, agents/ = the test
agents), the LisPy docs in ./lisppy-docs, and the host binding list in ./HOST-BINDINGS.py (the Python host that
will load your file; you may NOT change it). You have NO access to the real kernel's source and must not search
for it, fetch it, or reconstruct it from memory of any "brainstem.py" — derive the behaviour from expected.json.

## What the host gives your Lisp (read HOST-BINDINGS.py)
Your file is loaded with `run_string` into a lispy-core@1 default env plus these bindings:
(model-call messages tools) (model-call-safe messages tools) (json-parse-or-nil text) (agents-load) (agents-tools)
(agent-exists? name) (agent-perform name args) -> ("ok" text) | ("error" text), (soul) (soul-path) (model)
(version) (voice-mode) (uuid). The host then evaluates `(handle-chat *request*)` for POST /chat, `(handle-health)`
for GET /health and `(handle-version)` for GET /version; each must return (list status-code body-dict).
/chat's *request* is the parsed JSON body (a dict), or () when the body was not valid JSON.

## Rules
- LisPy only: begin/let*/cond/lambda/car/cdr/append/filter/make-dict/get/has-key?/string-append/string-join/
  string-trim/number->string/equal?/null?/dict?/list?/string?. NOTE: `(cons x host-list)` makes an improper pair in
  lispy — use `(append (list x) xs)`. `(or #f "")` is "" (Scheme). `get` of a missing key is ().
- Match expected.json EXACTLY: status codes, error strings, key sets, agent_logs format and order, the 3-round tool
  loop, the fallback sentence, the session_id echo, model/requested_model.
- Do not touch anything outside this directory. Do not install packages.

## Acceptance
1. python3 -c "import sys; sys.path.insert(0,'$HOME/Wildhaven/brainstem-distro-lab/lisppy'); import lisp; lisp.run_string(open('brainstem.lisp').read(), lisp.make_global_env()); print('parses and defines')"
   (binding names will be unbound at define time only if you call them at top level — define functions only.)
2. Dry-run the fake model yourself: start `python3 oracle/fake_model.py <port>` and write a tiny throwaway Python
   harness in THIS directory that loads lisp.py + HOST-BINDINGS.py-style bindings pointed at the fake, feeds each
   scenario from oracle/scenarios.py, and diffs against expected.json (the `keys` list is sorted(body.keys());
   for /health only code/status/agents are compared; for /version only code + presence of "version"; 404 only code).
   Report N of 25 identical. Iterate until 25/25.

## Report
End with: the final brainstem.lisp path, your harness's verbatim 25-line result, and a "Flags / surprises" section
(anything in expected.json you could not infer and had to guess; anything LisPy made awkward).
