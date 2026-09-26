# MultiSourcePriceOracle

A reusable multi-source price/data oracle primitive for GenLayer. Validators
independently fetch several web sources for the same asset, extract a
numeric price from each, and must agree with each other via the Equivalence
Principle before a round is accepted at all. Whether that agreed reading is
trusted immediately or has to sit through a dispute window is then decided
by plain, auditable code — not by asking the model whether the sources
"look consistent enough."

## Reviewer summary

- Register a feed with 2+ independent source URLs, a threshold, and a
  dispute window.
- `request_update()` has every validator fetch all sources, extract a
  price from each via `gl.nondet.exec_prompt(..., response_format="json")`,
  and agree with each other via `gl.eq_principle.prompt_comparative`.
- The spread across sources is then computed in plain Python. Within
  tolerance → finalizes immediately. Beyond tolerance → opens a dispute
  window instead of trusting it outright.
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
and a record of the bugs found across two self-review passes.
