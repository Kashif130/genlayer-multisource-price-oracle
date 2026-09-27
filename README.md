# MultiSourcePriceOracle

A reusable multi-source price/data oracle primitive for GenLayer. Validators
independently fetch several web sources for the same asset, extract a
numeric price from each, and reach consensus via the Equivalence Principle
on both the reading itself and on the decision it drives — whether that
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
  and reach consensus via `gl.eq_principle.prompt_comparative`.
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

## Why the decision lives inside the equivalence check

Per-source price extraction is allowed the usual small (~1%) slack between
validators — real web text and LLM extraction naturally jitter that much,
and it's harmless if it stays confined to the audit-trail readings. What
must **not** be allowed to drift between validators is the round's actual
fate: the exact price it publishes, and whether it finalizes or opens a
dispute window. An earlier draft computed those two things in plain code
*after* the equivalence check returned — using numbers that had only been
checked for ~1% agreement. That meant a different, equally "close enough"
validator could have led the same call and legitimately produced a
different published price and a different finalize/dispute outcome for
identical real-world input: which validator happened to lead was silently
deciding the round's fate, not the market data.

The fix moves the median/spread/status derivation inside the same closure
the Equivalence Principle checks, and requires validators to match
**exactly** on the four values that drive state — enough-sources, the
canonical (rounded) price, the spread, and the status — while still
allowing the usual slack on the raw per-source numbers those are derived
from. A round now only reaches consensus at all when validator-compatible
runs would have produced the same final price and the same lifecycle
outcome; a run that would have diverged on either one correctly fails to
reach consensus instead of silently picking one. See DECISION.md's
"Self-review, pass 3" entry for the full account.

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
