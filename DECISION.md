# MultiSourcePriceOracle Decision Record

## The product

A price/data oracle any GenLayer dApp can register a feed against, instead
of every project rolling its own multi-source-fetch-and-trust logic. The
job is narrow on purpose: aggregate several sources into one number,
disagree loudly (via a dispute window) when the sources disagree, and keep
a full audit trail of how each round's price was reached.

## Why a dispute window, not just "average and trust it"

Averaging silently hides disagreement. If one source is stale or wrong, an
average still produces a confident-looking number with no signal anything
was off. The dispute window makes disagreement visible and actionable:
below the threshold, the aggregate is trusted immediately (no friction for
the common case); above it, the round pauses and lets anyone — not just
the feed owner — challenge it with evidence before it's treated as truth.

## Why the deviation check is plain code, not another LLM call

The spread-across-sources calculation (max − min, as a percentage of the
median) runs on numbers the model already extracted, after the
equivalence-checked fetch step returns. It would have been possible to
ask the model "is this spread too large," but that turns a comparison of
numbers the contract already has into a non-deterministic, unauditable
judgment call. Keeping it in plain code means the dispute-trigger
threshold is exactly what a reviewer — or a dApp integrating this feed —
can read and verify, and it's configurable per feed via `deviation_bps`
rather than baked into a prompt.

## Why fetch-availability is forced by code, not merely requested by prompt

Earlier drafts asked the model to report whether each source was
fetchable. That's a prompt instruction, not a security boundary — the
same lesson this ecosystem's other reviewed contracts already apply to
similar checks. Here, `leader()` observes each fetch's actual outcome
directly (`page != "[FETCH_UNAVAILABLE]"`) and computes the `fetched` list
itself; the model is only ever asked to extract a number from page text
it was actually shown, never asked to self-report fetchability. A source
that failed to fetch is forced to `UNAVAILABLE` regardless of anything a
compromised or confused model output might claim.

## Why the dispute verdict defaults to UPHELD on ambiguity

The two mistakes a dispute verdict can make are asymmetric: wrongly
overturning a good price is a stronger, more consequential claim than
wrongly upholding a price that was actually fine (a downstream dApp keeps
using a number that was already trusted once). `judge()`'s own prompt
says so explicitly, and the code defaults any non-`OVERTURNED` /
malformed verdict to `UPHELD` rather than leaving it ambiguous.

## Why there's no protocol-level admin, only per-feed owners

A single contract-wide admin would mean every dApp depending on this
oracle also implicitly depends on trusting whoever deployed it. Instead,
`FeedConfig.owner` is set to whoever calls `register_feed`, and only that
feed's own configuration is owner-gated. Requesting an update, disputing a
round, and resolving a dispute are permissionless for every feed — no
single party has a lever over whether any given price round is trusted.

## Rejected alternative: let the model pick "the" final price directly

An earlier framing considered skipping the deterministic median/spread
step and just asking the model to pick a final price from all the raw
source text. Rejected because that's the "thin LLM wrapper" pattern this
category explicitly excludes — there'd be no real consensus logic, just
one opaque judgment call standing in for one. The model's job stays
narrow (extract a number from page text, or judge a dispute); the actual
oracle logic (aggregation, thresholding, workflow state) stays in code.

## Known limitation, not fixed: disputes have no resolution deadline

Once `open_dispute` is called, the round can sit `disputed` indefinitely
if nobody calls `resolve_dispute`. `resolve_dispute` is intentionally
unrestricted (any address, not just the disputer or feed owner, can call
it), which mitigates but doesn't eliminate this. A bonded resolver role or
an auto-revert-to-original-price timeout would close the gap — left out
here because it adds real complexity (stake accounting, slashing rules)
for a submission whose job is to demonstrate the core primitive, not ship
a production incentive system.

## Self-review, pass 1: bugs found before any reference contract existed

The first draft was checked against general GenVM storage-type rules.
Fixed at that point: `list[T]` → `DynArray[T]` for storage dataclass
fields; missing `@allow_storage` on stored dataclasses; an invalid
hand-typed placeholder address; a division-by-zero risk if a source
returned zero; missing bounds validation on feed config at registration.

## Self-review, pass 2: fixed after comparing against a verified-working reference contract

The project shipped with a `"Could not load contract schema"` error. A
second pass compared this contract line-by-line against a separate,
already-deployed GenLayer contract from the same ecosystem to find what
was actually different. Real bugs found and fixed:

1. **Dependency header was almost certainly the direct cause.** The first
   draft's `# { "Depends": "py-genlayer:latest" }` doesn't correspond to a
   resolvable dependency tag. Replaced with the exact pinned hash
   (`py-genlayer:1jb45aa8...`) taken from a contract confirmed to load and
   run successfully in this same environment.
2. **View methods returned raw custom dataclasses.** `get_round`/`get_feed`
   returned `PriceRound`/`FeedConfig` objects directly. The verified
   reference contract never does this anywhere — every `@gl.public.view`
   method returns a plain `dict`/`list` built field-by-field from JSON-safe
   types (`str`, `int`, `bool`). A schema generator that can't represent a
   nested custom-dataclass return type (especially one containing a
   `DynArray` of another custom dataclass, as `PriceRound.sources` did) is
   the most likely single explanation for a schema load failure. Both view
   methods now hand-build a `dict`.
3. **Wrong error-handling API entirely.** The first draft used
   `from genlayer import UserError` then `raise UserError(...)`. The
   verified reference contract uses `raise gl.vm.UserError(...)` with no
   separate import, throughout. Every raise site was switched, and adopted
   the reference's `[EXPECTED]`/`[TRANSIENT]`/`[LLM_ERROR]` message-prefix
   convention for distinguishing ordinary validation failures from
   transient infrastructure issues from malformed model output.
4. **Wrong current-time API entirely.** The first draft used
   `gl.message.datetime.timestamp()` (which doesn't exist) with `u256`
   epoch-second fields. The verified reference contract reads
   `gl.message_raw.get("datetime", "")`, which returns an ISO-8601 UTC
   string, and does its own date arithmetic with hand-written
   `_add_seconds`/ISO-string helpers (lexicographic string comparison
   already gives correct chronological ordering for a fixed-width
   ISO-8601 format). All time fields (`created_at`, `dispute_deadline`)
   were converted from `u256` to `str` and the helper copied over.
5. **`eq_principle.prompt_comparative` was doing manual string parsing.**
   The first draft encoded per-source results into a single `"::"`
   -delimited string and parsed it back out by hand. The verified
   reference contract instead has its non-deterministic function return a
   plain `dict`, obtained via `gl.nondet.exec_prompt(prompt,
   response_format="json")` for structured extraction, and reads fields
   straight off the dict `eq_principle.prompt_comparative` returns. Both
   `leader()` (price extraction) and `judge()` (dispute verdict) were
   rewritten around this — considerably less fragile than the string
   encoding, and it's what the ecosystem's own working code does.
6. **An unused, likely-nonexistent storage helper.** The first draft
   called `gl.storage.copy_to_memory()` before a non-deterministic
   closure, following an unverified doc example. The verified reference
   contract never calls this anywhere — it passes plain field values
   (already detached copies once read off a dataclass) straight into its
   closures. Replaced with a plain list comprehension over
   `feed.sources` before the closure is defined.
7. **Test time-travel cheatcode name was a guess.** The first draft's
   tests called a placeholder `direct_vm.advance_time(...)`. The verified
   reference project's `conftest.py` shows the real cheatcode is
   `direct_vm.warp(iso_string)` (paired with re-patching
   `gl.message_raw["datetime"]` so the contract's own clock reads move
   too) — copied over as the `warp_to()` helper.

## Self-review, pass 3: fixed after steward rejection (non-deterministic finalize/dispute outcome)

The steward rejected the submission because the round's decision logic (median price, spread_bps,
and the finalized/pending_dispute status) was computed by plain code *after* `eq_principle
.prompt_comparative` returned, from numbers that were only checked for ~1% agreement across
validators. That meant a different, equally "close enough" leader could legitimately have produced
a different exact price and a different finalize-vs-dispute outcome for the same real-world
inputs -- whichever validator happened to lead that call effectively decided the round's fate, not
the underlying market data.

Fixed by moving the median/spread/status derivation *inside* `leader()`, so it's part of what the
Equivalence Principle itself checks, and by rewriting the principle so per-source raw numbers keep
their ~1% audit-only slack while the four values that actually drive state (whether enough sources
were usable, the canonical rounded price, the spread, and the status) must match exactly across
validators. A round now only reaches consensus at all when validator-compatible runs agree on the
same final price and the same lifecycle outcome; runs that would have diverged on either one
correctly fail to reach consensus instead of silently picking one.

Three tests were added to lock this in: an exact-boundary case (`spread_bps == deviation_bps` must
finalize), a canonicalization case (the stored price is the rounded figure the check agreed on, not
a raw unrounded float), and a case where a fetched-but-unparseable source is excluded from the
price decision rather than aborting the round or being treated as a zero reading.

## Open items still worth a final check before submitting

- The pinned `py-genlayer:...` dependency hash was taken from a contract
  that loads successfully in this environment as of this writing, but if
  your Studio/CLI pins a different GenVM version, confirm the current tag
  via your toolchain rather than assuming this exact hash is universal.
- `DynArray[T]()` construction/`.append()` is used the same way the
  verified reference contract uses it — low remaining risk, but worth a
  quick sanity check on first deploy if the contract fails to compile.
