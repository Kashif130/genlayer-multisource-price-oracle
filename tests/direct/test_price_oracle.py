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
