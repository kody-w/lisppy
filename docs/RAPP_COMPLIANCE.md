# LisPy and the RAPP spine

LisPy follows the latest crawled RAPP spine as a **userspace agent cubby**. It
does not claim to be the RAPP kernel, a distro, or a substrate runtime.

Authoritative spine snapshot:

- repository: `kody-w/rapp-spine`
- spine: `rapp-spine/1.1`
- commit: `b41a4578104ebd7d837fe8022bc5f81acc6c377d`
- exhaustive crawl: 220/220 graph nodes visited, 90 required sources read,
  37 upstream sources unresolved

The machine-readable lock and compliance classification live in
[`rapp-compliance.json`](../rapp-compliance.json).

## Why this classification is required

The RAPP kernel is sacred. A conformant capability does not add an endpoint or
patch `brainstem.py`; it ships as a drop-in `*_agent.py` cartridge and rides the
existing `POST /chat` wire.

LisPy therefore ships:

- [`agents/lispy_runtime_agent.py`](../agents/lispy_runtime_agent.py), a
  `BasicAgent.metadata + perform(**kwargs) -> str` adapter;
- [`cubby.json`](../cubby.json), a `rapp-cubby/1.0` manifest marking only the
  agent anatomy as streamable;
- no Flask app, no `/chat` replacement, no auth server, and no RAPP runtime
  parity claim.

## Supported RAPP actions

The adapter exposes three safe operations through the frozen agent ABI:

1. `evaluate` runs source in the default safe `LispyVM` profile.
2. `contract_manifest` returns the installed Core contract identity.
3. `hosted_demo` runs the deterministic, credential-free hosted-frame proof.

Every `perform()` result is a string containing one
`lispy-rapp-agent-result/1.0` JSON object. Extra kwargs are accepted, preserving
the forward-compatible RAPP agent ABI. The adapter never enables LisPy's
trusted filesystem or Python-process capabilities.

## Install into a RAPP brainstem

Install LisPy on the host, then stream or copy the agent through the normal RAPP
userspace path:

```bash
python3 -m pip install .
cp agents/lispy_runtime_agent.py ~/.brainstem/src/agents/
```

The next `/chat` request discovers the agent; no kernel restart or route change
is required. In a cubby-aware host, use the repository's `cubby.json` and normal
sha256-verified cubby loading instead of copying manually.

## Compliance matrix

| Spine contract | LisPy status |
|---|---|
| `rapp-agent/1.0` | Conformant userspace adapter |
| `rapp-cubby/1.0` | Conformant manifest and streamable-agent declaration |
| `rapp-canon/1.0` | Spine currency pinned; no higher-tier legality claim |
| `rapp-substrate-trust/1.0` | Public read-only artifact; no secrets |
| `rapp-runtime-parity/1.0` | Not applicable; no `/chat` runtime claim |
| `rapp-auth/1.0` | Not applicable; no token exchange |
| `rapp-kernel-boundary/1.0` | Not applicable; no listening service/routes |
| `rapp-trust/1.0` | Not applicable; no rappid minting/actor authorization |

## Open registration debt

The code and cubby are locally compliant, but LisPy is not yet registered in
the latest spine, RAR, or another canonical RAPP registry. Per `rapp-canon/1.0`,
that is a currency/registration task, not permission to edit the kernel or
claim runtime parity.
