from pathlib import Path

import pytest

from conftest import warp_to

ASSET = "ETH-USD"
SOURCE_A = "https://example.com/source-a"
SOURCE_B = "https://example.com/source-b"
EVIDENCE_URL = "https://example.com/evidence"
PARSE_HINT = "Look for the USD spot price."

NOW = "2099-01-01T00:00:00Z"
SHORT_WINDOW = 600          # 10 minutes -- within MIN/MAX bounds
AFTER_WINDOW = "2099-01-01T00:20:00Z"   # > 10 minutes after NOW
JUST_BEFORE_WINDOW = "2099-01-01T00:09:59Z"


def register_default_feed(contract, direct_vm, deviation_bps=50, window_seconds=SHORT_WINDOW):
    direct_vm.sender = direct_vm.sender  # no-op, keeps caller explicit at call sites below
    contract.register_feed(ASSET, [SOURCE_A, SOURCE_B], PARSE_HINT, deviation_bps, window_seconds)


def mock_price_sources(direct_vm, body_a, body_b, prices_json):
    direct_vm.clear_mocks()
    direct_vm.mock_web(r"https://example\.com/source-a", {"status": 200, "body": body_a})
    direct_vm.mock_web(r"https://example\.com/source-b", {"status": 200, "body": body_b})
    direct_vm.mock_llm(r".*extracting the current numeric spot price.*", prices_json)


def mock_dispute_evidence(direct_vm, evidence_body, verdict_json):
    direct_vm.mock_web(r"https://example\.com/evidence", {"status": 200, "body": evidence_body})
    direct_vm.mock_llm(r".*oracle proposed a price of.*", verdict_json)


# --- feed registration ---

def test_register_feed(contract, direct_vm, direct_alice):
    direct_vm.sender = direct_alice
    register_default_feed(contract, direct_vm)
    feed = contract.get_feed(ASSET)
    assert feed["active"] is True
    assert feed["sources"] == [SOURCE_A, SOURCE_B]
    assert feed["owner"] == str(direct_alice)


def test_register_feed_twice_fails(contract, direct_vm, direct_alice):
    direct_vm.sender = direct_alice
    register_default_feed(contract, direct_vm)
    with pytest.raises(Exception):
        register_default_feed(contract, direct_vm)


def test_register_feed_rejects_too_few_sources(contract, direct_vm, direct_alice):
    direct_vm.sender = direct_alice
    with pytest.raises(Exception):
        contract.register_feed(ASSET, [SOURCE_A], PARSE_HINT, 50, SHORT_WINDOW)


def test_register_feed_rejects_bad_url(contract, direct_vm, direct_alice):
    direct_vm.sender = direct_alice
    with pytest.raises(Exception):
        contract.register_feed(ASSET, [SOURCE_A, "not-a-url"], PARSE_HINT, 50, SHORT_WINDOW)


def test_register_feed_rejects_deviation_out_of_range(contract, direct_vm, direct_alice):
    direct_vm.sender = direct_alice
    with pytest.raises(Exception):
        contract.register_feed(ASSET, [SOURCE_A, SOURCE_B], PARSE_HINT, 0, SHORT_WINDOW)
    with pytest.raises(Exception):
        contract.register_feed(ASSET, [SOURCE_A, SOURCE_B], PARSE_HINT, 20000, SHORT_WINDOW)


def test_register_feed_rejects_window_out_of_range(contract, direct_vm, direct_alice):
    direct_vm.sender = direct_alice
    with pytest.raises(Exception):
        contract.register_feed(ASSET, [SOURCE_A, SOURCE_B], PARSE_HINT, 50, 10)


def test_set_feed_active_requires_owner(contract, direct_vm, direct_alice, direct_bob):
    direct_vm.sender = direct_alice
    register_default_feed(contract, direct_vm)
    direct_vm.sender = direct_bob
    with pytest.raises(Exception):
        contract.set_feed_active(ASSET, False)


# --- happy path: sources agree ---

def test_finalizes_when_sources_agree(contract, direct_vm, direct_alice):
    direct_vm.sender = direct_alice
    register_default_feed(contract, direct_vm, deviation_bps=50)
    warp_to(direct_vm, NOW)

    mock_price_sources(
        direct_vm,
        "ETH price: $3000.00",
        "ETH is trading at 3005 USD",
        '{"prices": [3000, 3005]}',
    )
    contract.request_update(ASSET)

    round_data = contract.get_round(ASSET, 1)
    assert round_data["status"] == "finalized"
    assert contract.get_latest_price(ASSET) != ""
    assert contract.get_latest_round_id(ASSET) == 1


# --- deviation triggers a dispute window ---

def test_opens_dispute_window_when_sources_disagree(contract, direct_vm, direct_alice):
    direct_vm.sender = direct_alice
    register_default_feed(contract, direct_vm, deviation_bps=50)  # 0.5% threshold
    warp_to(direct_vm, NOW)

    mock_price_sources(
        direct_vm,
        "ETH price: $3000.00",
        "ETH price: $3600.00",
        '{"prices": [3000, 3600]}',
    )
    contract.request_update(ASSET)

    round_data = contract.get_round(ASSET, 1)
    assert round_data["status"] == "pending_dispute"
    assert contract.get_latest_price(ASSET) == ""


def test_finalizes_at_exact_deviation_boundary(contract, direct_vm, direct_alice):
    # spread_bps == deviation_bps must finalize (the decision uses "<=", not "<"), and this value
    # must come from the same canonical figure that's stored -- not a separately recomputed one.
    direct_vm.sender = direct_alice
    register_default_feed(contract, direct_vm, deviation_bps=100)  # exactly 1.00% threshold
    warp_to(direct_vm, NOW)

    # median 200, spread (201-199)/200*10000 = exactly 100 bps -- equal to the threshold.
    mock_price_sources(
        direct_vm,
        "price 199",
        "price 201",
        '{"prices": [199, 201]}',
    )
    contract.request_update(ASSET)

    round_data = contract.get_round(ASSET, 1)
    assert round_data["spread_bps"] == 100
    assert round_data["status"] == "finalized"


def test_price_is_canonicalized_to_six_decimals(contract, direct_vm, direct_alice):
    # The median can land on a long repeating decimal (e.g. an odd split across sources).
    # The stored price must be the rounded, canonical figure the equivalence check agreed on --
    # not a raw, differently-formatted float that would break exact cross-validator matching.
    direct_vm.sender = direct_alice
    register_default_feed(contract, direct_vm, deviation_bps=50)
    warp_to(direct_vm, NOW)

    mock_price_sources(
        direct_vm,
        "price 10",
        "price 10.0000005",
        '{"prices": [10, 10.0000005]}',
    )
    contract.request_update(ASSET)

    round_data = contract.get_round(ASSET, 1)
    assert round_data["price"] == str(round((10 + 10.0000005) / 2, 6))


def test_ignores_non_numeric_extraction_from_fetched_source(contract, direct_vm, direct_alice):
    # A source can be fetched successfully but still yield no usable number (garbage/ambiguous
    # page text). That source must be excluded from the price decision entirely, not treated as
    # a usable reading of 0 or as a reason to abort when enough *other* sources are usable.
    direct_vm.sender = direct_alice
    contract.register_feed(
        ASSET, [SOURCE_A, SOURCE_B, "https://example.com/source-c"], PARSE_HINT, 50, SHORT_WINDOW
    )
    warp_to(direct_vm, NOW)

    direct_vm.clear_mocks()
    direct_vm.mock_web(r"https://example\.com/source-a", {"status": 200, "body": "price 3000"})
    direct_vm.mock_web(r"https://example\.com/source-b", {"status": 200, "body": "price 3005"})
    direct_vm.mock_web(r"https://example\.com/source-c", {"status": 200, "body": "no clear number here"})
    direct_vm.mock_llm(
        r".*extracting the current numeric spot price.*",
        '{"prices": [3000, 3005, "UNAVAILABLE"]}',
    )
    contract.request_update(ASSET)

    round_data = contract.get_round(ASSET, 1)
    assert round_data["status"] == "finalized"
    assert round_data["sources"][2]["usable"] is False
    assert round_data["sources"][0]["usable"] is True
    assert round_data["sources"][1]["usable"] is True


def test_fewer_than_two_usable_sources_aborts(contract, direct_vm, direct_alice):
    direct_vm.sender = direct_alice
    register_default_feed(contract, direct_vm)
    warp_to(direct_vm, NOW)

    mock_price_sources(
        direct_vm,
        "no price on this page",
        "no price on this page",
        '{"prices": ["UNAVAILABLE", "UNAVAILABLE"]}',
    )
    with pytest.raises(Exception):
        contract.request_update(ASSET)


# --- dispute flow ---

def test_dispute_upheld_keeps_original_price(contract, direct_vm, direct_alice, direct_bob):
    direct_vm.sender = direct_alice
    register_default_feed(contract, direct_vm, deviation_bps=10)
    warp_to(direct_vm, NOW)
    mock_price_sources(direct_vm, "price 3000", "price 3200", '{"prices": [3000, 3200]}')
    contract.request_update(ASSET)

    direct_vm.sender = direct_bob
    contract.open_dispute(ASSET, 1, EVIDENCE_URL)

    mock_dispute_evidence(direct_vm, "confirms 3000 is correct", '{"verdict": "UPHELD", "corrected_price": null}')
    contract.resolve_dispute(ASSET, 1)

    round_data = contract.get_round(ASSET, 1)
    assert round_data["status"] == "resolved"
    assert round_data["final_price"] == round_data["price"]
    assert contract.get_latest_price(ASSET) == round_data["final_price"]


def test_dispute_overturned_updates_price(contract, direct_vm, direct_alice, direct_bob):
    direct_vm.sender = direct_alice
    register_default_feed(contract, direct_vm, deviation_bps=10)
    warp_to(direct_vm, NOW)
    mock_price_sources(direct_vm, "price 3000", "price 3200", '{"prices": [3000, 3200]}')
    contract.request_update(ASSET)

    direct_vm.sender = direct_bob
    contract.open_dispute(ASSET, 1, EVIDENCE_URL)

    mock_dispute_evidence(
        direct_vm, "actual price was 3150",
        '{"verdict": "OVERTURNED", "corrected_price": 3150}',
    )
    contract.resolve_dispute(ASSET, 1)

    round_data = contract.get_round(ASSET, 1)
    assert round_data["status"] == "resolved"
    assert round_data["final_price"] == "3150.0"
    assert contract.get_latest_price(ASSET) == "3150.0"
    assert round_data["dispute_rationale"] == "OVERTURNED: corrected price 3150.0 replaces proposed " + round_data["price"]


def test_cannot_dispute_a_finalized_round(contract, direct_vm, direct_alice, direct_bob):
    direct_vm.sender = direct_alice
    register_default_feed(contract, direct_vm, deviation_bps=50)
    warp_to(direct_vm, NOW)
    mock_price_sources(direct_vm, "price 3000", "price 3005", '{"prices": [3000, 3005]}')
    contract.request_update(ASSET)

    direct_vm.sender = direct_bob
    with pytest.raises(Exception):
        contract.open_dispute(ASSET, 1, EVIDENCE_URL)


def test_cannot_dispute_after_window_closes(contract, direct_vm, direct_alice, direct_bob):
    direct_vm.sender = direct_alice
    register_default_feed(contract, direct_vm, deviation_bps=10, window_seconds=SHORT_WINDOW)
    warp_to(direct_vm, NOW)
    mock_price_sources(direct_vm, "price 3000", "price 3200", '{"prices": [3000, 3200]}')
    contract.request_update(ASSET)

    warp_to(direct_vm, AFTER_WINDOW)
    direct_vm.sender = direct_bob
    with pytest.raises(Exception):
        contract.open_dispute(ASSET, 1, EVIDENCE_URL)


# --- expiry / finalize_if_expired ---

def test_finalize_if_expired_rejects_early_call(contract, direct_vm, direct_alice):
    direct_vm.sender = direct_alice
    register_default_feed(contract, direct_vm, deviation_bps=10, window_seconds=SHORT_WINDOW)
    warp_to(direct_vm, NOW)
    mock_price_sources(direct_vm, "price 3000", "price 3200", '{"prices": [3000, 3200]}')
    contract.request_update(ASSET)

    warp_to(direct_vm, JUST_BEFORE_WINDOW)
    with pytest.raises(Exception):
        contract.finalize_if_expired(ASSET, 1)


def test_finalize_if_expired_succeeds_after_window(contract, direct_vm, direct_alice):
    direct_vm.sender = direct_alice
    register_default_feed(contract, direct_vm, deviation_bps=10, window_seconds=SHORT_WINDOW)
    warp_to(direct_vm, NOW)
    mock_price_sources(direct_vm, "price 3000", "price 3200", '{"prices": [3000, 3200]}')
    contract.request_update(ASSET)

    warp_to(direct_vm, AFTER_WINDOW)
    contract.finalize_if_expired(ASSET, 1)

    round_data = contract.get_round(ASSET, 1)
    assert round_data["status"] == "finalized"
    assert contract.get_latest_price(ASSET) == round_data["price"]


# --- error handling ---

def test_unknown_feed_reverts(contract):
    with pytest.raises(Exception):
        contract.request_update("DOES-NOT-EXIST")


def test_unknown_round_reverts(contract, direct_vm, direct_alice):
    direct_vm.sender = direct_alice
    register_default_feed(contract, direct_vm)
    with pytest.raises(Exception):
        contract.get_round(ASSET, 999)


# --- exact consensus binding (steward feedback) -----------------------------------

CONTRACT_SRC = Path(__file__).resolve().parents[2] / "contracts" / "price_oracle.py"


def test_consensus_is_exact_not_comparative():
    # Static guard: no tolerance-based equivalence principle may be used for any published value.
    src = CONTRACT_SRC.read_text()
    assert "prompt_comparative" not in src
    assert src.count("gl.eq_principle.strict_eq(") == 2  # one per boundary: round + dispute


def test_per_source_readings_are_canonical_strings(contract, direct_vm, direct_alice):
    # Stored readings come from the consensus-bound canonical result, not the raw model output.
    direct_vm.sender = direct_alice
    register_default_feed(contract, direct_vm, deviation_bps=50)
    warp_to(direct_vm, NOW)
    mock_price_sources(
        direct_vm, "price 3000", "price 3005", '{"prices": [3000.0000004, 3005]}'
    )
    contract.request_update(ASSET)

    r = contract.get_round(ASSET, 1)
    assert [s["raw_value"] for s in r["sources"]] == ["3000.0", "3005.0"]
    assert r["price"] == "3002.5"
    assert r["final_price"] == "3002.5"
    assert contract.get_latest_price(ASSET) == "3002.5"


def test_boolean_and_non_finite_readings_are_not_usable(contract, direct_vm, direct_alice):
    direct_vm.sender = direct_alice
    contract.register_feed(
        ASSET, [SOURCE_A, SOURCE_B, "https://example.com/source-c"], PARSE_HINT, 50, SHORT_WINDOW
    )
    warp_to(direct_vm, NOW)
    direct_vm.clear_mocks()
    direct_vm.mock_web(r"https://example\.com/source-a", {"status": 200, "body": "x"})
    direct_vm.mock_web(r"https://example\.com/source-b", {"status": 200, "body": "x"})
    direct_vm.mock_web(r"https://example\.com/source-c", {"status": 200, "body": "x"})
    direct_vm.mock_llm(
        r".*extracting the current numeric spot price.*",
        '{"prices": [true, 3000, 3005]}',
    )
    contract.request_update(ASSET)

    r = contract.get_round(ASSET, 1)
    assert [s["usable"] for s in r["sources"]] == [False, True, True]
    assert r["sources"][0]["raw_value"] == "UNAVAILABLE"
    assert r["price"] == "3002.5"


def test_dispute_final_price_is_canonical_exact_value(contract, direct_vm, direct_alice, direct_bob):
    # The published final price is the canonical figure inside the agreed result: a corrected
    # price with sub-precision noise is published as its exact 6-decimal canonical form.
    direct_vm.sender = direct_alice
    register_default_feed(contract, direct_vm, deviation_bps=10)
    warp_to(direct_vm, NOW)
    mock_price_sources(direct_vm, "price 3000", "price 3200", '{"prices": [3000, 3200]}')
    contract.request_update(ASSET)
    direct_vm.sender = direct_bob
    contract.open_dispute(ASSET, 1, EVIDENCE_URL)

    mock_dispute_evidence(
        direct_vm, "actual price was 3150.1234564",
        '{"verdict": "OVERTURNED", "corrected_price": 3150.1234564}',
    )
    contract.resolve_dispute(ASSET, 1)

    r = contract.get_round(ASSET, 1)
    assert r["status"] == "resolved"
    assert r["final_price"] == "3150.123456"
    assert contract.get_latest_price(ASSET) == "3150.123456"


def test_overturn_without_usable_price_keeps_original(contract, direct_vm, direct_alice, direct_bob):
    # An OVERTURNED verdict that carries no usable corrected price cannot change the price; the
    # outcome recorded is UPHELD, decided inside the consensus-bound result.
    direct_vm.sender = direct_alice
    register_default_feed(contract, direct_vm, deviation_bps=10)
    warp_to(direct_vm, NOW)
    mock_price_sources(direct_vm, "price 3000", "price 3200", '{"prices": [3000, 3200]}')
    contract.request_update(ASSET)
    direct_vm.sender = direct_bob
    contract.open_dispute(ASSET, 1, EVIDENCE_URL)

    mock_dispute_evidence(
        direct_vm, "unclear", '{"verdict": "OVERTURNED", "corrected_price": "n/a"}'
    )
    contract.resolve_dispute(ASSET, 1)

    r = contract.get_round(ASSET, 1)
    assert r["final_price"] == r["price"]
    assert r["dispute_rationale"].startswith("UPHELD")


def test_dispute_rationale_is_built_in_code_not_model_text(contract, direct_vm, direct_alice, direct_bob):
    direct_vm.sender = direct_alice
    register_default_feed(contract, direct_vm, deviation_bps=10)
    warp_to(direct_vm, NOW)
    mock_price_sources(direct_vm, "price 3000", "price 3200", '{"prices": [3000, 3200]}')
    contract.request_update(ASSET)
    direct_vm.sender = direct_bob
    contract.open_dispute(ASSET, 1, EVIDENCE_URL)

    mock_dispute_evidence(
        direct_vm, "ok",
        '{"verdict": "UPHELD", "corrected_price": null, "rationale": "MODEL FREE TEXT"}',
    )
    contract.resolve_dispute(ASSET, 1)
    assert "MODEL FREE TEXT" not in contract.get_round(ASSET, 1)["dispute_rationale"]
