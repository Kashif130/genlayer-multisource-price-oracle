# MultiSourcePriceOracle

A reusable multi-source price/data oracle primitive for GenLayer. Validators
independently fetch several web sources for the same asset, extract a
numeric price from each, and reach consensus via `gl.eq_principle.strict_eq`
on both the readings and on the decision they drive — whether that
reading is trusted immediately or has to sit through a dispute window.
That decision (spread vs. threshold) is plain, auditable arithmetic, not a
model judgment call, but it's derived *inside* the same equivalence-checked
step, so every validator-compatible run of `request_update()` is guaranteed
to land on the same final price and the same finalize/dispute outcome —
see "Why the decision lives inside the equivalence check" below.

## Reviewer summary

- Register a feed with 2+ independent source URLs, a threshold, and a
  dispute window.
- `request_update()` has every validator fetch all sources, extract a
  price from each via `gl.nondet.exec_prompt(..., response_format="json")`,
  and reach consensus via `gl.eq_principle.strict_eq` over a canonical result.
- That consensus step itself computes the spread across sources in plain
  Python and decides the round's status: within tolerance → finalizes
  immediately; beyond tolerance → opens a dispute window instead of
  trusting it outright. See "Why the decision lives inside the
  equivalence check" for why this isn't done after consensus.
- Anyone can challenge a pending round with evidence
  (`open_dispute`/`resolve_dispute`), or let it auto-finalize once the
  window passes unchallenged (`finalize_if_expired`).
- Full round history — every source's raw reading, the computed spread,
  any dispute evidence and verdict — stays in state for audit.

## Architecture

```
proof/
├── contracts/
│   └── price_oracle.py        # the Intelligent Contract
├── tests/direct/
│   ├── conftest.py             # direct-mode fixtures (deploy, warp_to time-travel helper)
│   └── test_price_oracle.py    # gltest direct-mode test suite
├── artifacts/                  # populated by the GenLayer CLI on deploy - see .gitkeep
├── gltest.config.yaml
├── DEPLOYMENT.md                # source hash + checklist for the deployed-source evidence
├── DECISION.md                  # design rationale, rejected alternatives, self-review fixes
└── README.md                    # this file
```

### Contract methods

**Feed management** (owner-only per feed — whoever registers a feed owns it)
- `register_feed(asset_id, sources, parse_hint, deviation_bps, dispute_window_seconds)`
- `set_feed_active(asset_id, active)`

**Core oracle flow** (permissionless)
- `request_update(asset_id)` — fetch, extract, agree, then finalize or open
  a dispute window depending on spread.
- `open_dispute(asset_id, round_id, evidence_url)` — while the window is open.
- `resolve_dispute(asset_id, round_id)` — validators weigh the evidence and
  reach an UPHELD/OVERTURNED verdict.
- `finalize_if_expired(asset_id, round_id)` — anyone can push a round past
  an unchallenged window so it never gets stuck.

**Views** (all return plain `dict`/`str`/`int`/`bool`, never a raw custom
dataclass — see DECISION.md for why that distinction matters)
- `get_latest_price(asset_id)`, `get_latest_round_id(asset_id)`
- `get_feed(asset_id)`, `get_round(asset_id, round_id)`

## Round lifecycle

```
request_update()
      │
      ▼
 spread <= threshold ──────────────► status = finalized
      │
      ▼ (spread > threshold)
 status = pending_dispute (dispute window opens)
      │
      ├── nobody disputes in time ──► finalize_if_expired() → finalized
      │
      └── open_dispute(evidence) ──► status = disputed
                                            │
                                            ▼
                               resolve_dispute() → status = resolved
                               (verdict: UPHELD or OVERTURNED)
```

## Why there is no protocol-level admin

Each feed is owned by whoever registered it (`FeedConfig.owner`), not by a
single contract-wide owner. Anyone can stand up their own feed for their
own dApp without depending on, or trusting, whoever deployed this contract.
Only that feed's own configuration (active/inactive, essentially) is
gated; the oracle's actual output — requesting an update, disputing it,
resolving a dispute — is permissionless for every feed, so no single party
ever has a lever over whether a price round is trusted.

## Why every published value is bound exactly

An earlier revision allowed ~1% validator-to-validator slack and published whichever value the
leader picked (the dispute path in particular accepted corrected prices within 1% and stored the
leader's exact number; an even earlier version computed price and outcome outside consensus). Now
**every** price and lifecycle outcome the contract publishes is part of a `gl.eq_principle.strict_eq`
result and must match exactly across validators:

- `request_update()`: per-source readings (canonical 6-decimal strings or `"UNAVAILABLE"`), the
  fetched/usable flags, the median price, `spread_bps` and the `finalized` / `pending_dispute` /
  `aborted` outcome. The contract afterwards only copies fields out of the agreed result.
- `resolve_dispute()`: the UPHELD/OVERTURNED verdict **and** the exact final price. Unreadable
  evidence, malformed verdicts, or an overturn without a usable corrected price are forced to
  UPHELD in code. No model free text is stored; the rationale is built in code.
- `finalize_if_expired()`: clock + stored state only.

Each `strict_eq` sits in a module-level boundary function with no `self` and no storage access;
state is written only after it returns. Trade-off: if validators extract different numbers, the
round does not reach consensus and is simply retried, rather than publishing a leader-chosen value.
See DECISION.md "Self-review, pass 4".

## Running this project

```bash
pip install genlayer-test

# direct-mode tests: no Docker, no Studio, runs in-process with mocked
# web/LLM responses
pytest tests/direct -v
# or
gltest tests/direct/test_price_oracle.py

# deploy to a real network once ready
genlayer network set studionet   # or localnet / testnet-asimov
genlayer deploy --contract contracts/price_oracle.py
```

Example call sequence once deployed:

```
register_feed(
  "ETH-USD",
  ["https://example.com/eth-price-source-a", "https://example.com/eth-price-source-b"],
  "Look for the USD spot price.",
  50,     # 0.5% spread threshold
  600     # 10 minute dispute window
)
request_update("ETH-USD")
```

Force a dispute scenario by pointing one source at a page with a
deliberately different number, and confirm the round lands in
`pending_dispute` instead of `finalized`.

## Scope of this submission

This targets the core primitive — multi-source aggregation, equivalence-
checked consensus, deterministic dispute triggering, full audit trail — as
a standalone Intelligent Contract, not a full product with a frontend.

## Honest limitations

- **No dispute resolution deadline.** Once `open_dispute` is called, a
  round can sit `disputed` indefinitely if nobody calls `resolve_dispute`.
  `resolve_dispute` is intentionally unrestricted (any address can call
  it), which mitigates but doesn't eliminate this. See DECISION.md.
- **No economic bonding.** Disputing costs nothing but gas, so a feed
  under active griefing could see every borderline round disputed. A
  staked/slashed disputer role would close this gap; left out here to keep
  the submission focused on the core consensus primitive.
- **Basic URL validation only.** `_require_safe_url` checks shape (http(s)
  scheme, no embedded credentials, no control characters) but doesn't do
  the fuller DNS-resolution/redirect-status SSRF hardening this ecosystem
  has reviewed elsewhere, since feed registration here is owner-scoped
  rather than fully permissionless. A permissionless-registration variant
  of this contract should add that hardening.

See `DECISION.md` for the full design rationale, rejected alternatives,
and a record of the bugs found across three self-review/review passes.
