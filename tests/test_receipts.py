"""
Tests for receipts.py — consuming x402-receipts/v0.3 as accounting source docs.

Fixtures mirror the real Receipt shape from StelarDigital/x402-receipts
(atomic-unit amount strings, request/response/goods/delivery blocks).
"""
import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from counterralib import receipts as rc


def _receipt(**over):
    base = {
        "scheme": "x402-receipts/v0",
        "payment": {
            "chain_id": 8453,
            "tx_hash": "0x" + "a" * 64,
            "asset": "USDC",
            "amount": "1000000",           # 1.000000 USDC atomic (6 decimals)
            "payer": "0x" + "b" * 40,
            "payee": "0x" + "c" * 40,
        },
        "request": {
            "method": "GET",
            "url_hash": "abc",
            "params_hash": "def",
            "ts": "2026-07-25T12:00:00.000Z",
            "payment_requirements_sha256": "0xreqhash",
        },
        "response": {
            "status": 200,
            "body_sha256": "0xbodyhash",
            "content_type": "application/json",
            "ts": "2026-07-25T12:00:00.500Z",
            "latency_ms": 500,
        },
        "seller": {"erc8004_agent_id": "erc8004:8453:0x1234", "sig": None},
        "buyer": {"countersig": None},
        "anchor": None,
        "goods": {"description": "BTC-USD signal", "kind": "api-response",
                  "summary": None, "body_sha256": "0xbodyhash", "bytes": 42, "preview": None},
    }
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            base[k] = {**base[k], **v}
        else:
            base[k] = v
    return base


def test_amount_atomic_to_usd():
    assert rc.amount_to_usd("1000000", "USDC") == 1.0
    assert rc.amount_to_usd("3000", "USDC") == 0.003
    assert rc.amount_to_usd("15000000", "USDC") == 15.0
    print("atomic->USD conversion OK")


def test_amount_respects_decimals_hint():
    # 1 DAI = 1e18 atomic
    assert rc.amount_to_usd("1000000000000000000", "DAI") == 1.0
    print("decimals lookup OK")


def test_basic_journal_entry():
    j = rc.receipt_to_journal(_receipt())
    assert j["amount_usd"] == 1.0
    assert j["credit_account"].startswith("1085")
    assert j["delivery_status"] == "delivered"
    assert j["http_status"] == 200
    assert j["latency_ms"] == 500
    assert j["bookable"] is True
    assert j["tx_hash"] == "0x" + "a" * 64
    print("basic journal entry OK")


def test_registry_names_payee_and_category():
    reg = {("0x" + "c" * 40): {"label": "blockrun.ai", "category": "Market data"}}
    exp = {"Market data": "6410 - Data & Research Services"}
    j = rc.receipt_to_journal(_receipt(), registry=reg, expense_accounts=exp)
    assert j["provider"] == "blockrun.ai"
    assert j["category"] == "Market data"
    assert j["debit_account"].startswith("6410")
    print("registry naming + account mapping OK")


def test_failed_delivery_is_not_bookable():
    j = rc.receipt_to_journal(_receipt(delivery={"status": "failed"}))
    assert j["bookable"] is False
    assert "failed" in j["exception_reason"]
    print("failed delivery -> exception OK")


def test_partial_delivery_flagged():
    j = rc.receipt_to_journal(_receipt(delivery={"status": "partial"}))
    assert j["bookable"] is False
    assert "partial" in j["exception_reason"]
    print("partial delivery -> exception OK")


def test_http_error_not_bookable():
    j = rc.receipt_to_journal(_receipt(response={"status": 500}))
    assert j["bookable"] is False
    assert "500" in j["exception_reason"]
    print("http error -> exception OK")


def test_category_inferred_from_goods_kind():
    j = rc.receipt_to_journal(_receipt(goods={"kind": "dataset", "description": "x",
                                              "summary": None, "body_sha256": "h",
                                              "bytes": 1, "preview": None}))
    assert j["category"] == "Data & Research Services"
    print("goods.kind -> category OK")


def test_rejects_non_receipt():
    try:
        rc.receipt_to_journal({"scheme": "something-else"})
        assert False, "should have raised"
    except rc.ReceiptError:
        pass
    print("non-receipt rejected OK")


def test_reconcile_agreement():
    ev = SimpleNamespace(tx_hash="0x" + "a" * 64, amount_usdc=1.0,
                         payee_wallet="0x" + "c" * 40)
    assert rc.reconcile_with_settlement(_receipt(), ev) == []
    print("reconcile agreement OK")


def test_reconcile_amount_mismatch():
    ev = SimpleNamespace(tx_hash="0x" + "a" * 64, amount_usdc=2.0,
                         payee_wallet="0x" + "c" * 40)
    probs = rc.reconcile_with_settlement(_receipt(), ev)
    assert any("amount mismatch" in p for p in probs), probs
    print("reconcile amount mismatch OK")


def test_reconcile_txhash_and_payee_mismatch():
    ev = SimpleNamespace(tx_hash="0x" + "9" * 64, amount_usdc=1.0,
                         payee_wallet="0x" + "d" * 40)
    probs = rc.reconcile_with_settlement(_receipt(), ev)
    assert any("tx_hash mismatch" in p for p in probs)
    assert any("payee mismatch" in p for p in probs)
    print("reconcile tx/payee mismatch OK")


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
    print("\nALL RECEIPT TESTS PASSED")


def test_v051_asset_address_resolves_decimals():
    """v0.5.1 alignment: a contract-bound Base USDC receipt resolves to 6 decimals."""
    r = _receipt(payment={"asset": "SOMETHING_ODD",
                          "asset_address": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
                          "amount": "3000"})
    j = rc.receipt_to_journal(r)
    assert j["amount_usd"] == 0.003, j["amount_usd"]
    print("v0.5.1 asset_address decimals OK")


def test_v051_full_shape_still_books():
    """A full v0.5.1-shaped receipt (asset_address, goods, delivery) books cleanly."""
    r = _receipt(
        payment={"asset": "USDC",
                 "asset_address": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
                 "amount": "15000000"},
        delivery={"status": "delivered"})
    j = rc.receipt_to_journal(r)
    assert j["amount_usd"] == 15.0 and j["bookable"] is True
    print("v0.5.1 full-shape books OK")


def test_unverified_receipt_is_exception():
    """CAAP-1 v1.1: verified=False must block booking even if delivery is clean."""
    r = _receipt()  # delivered/200
    assert rc.receipt_to_journal(r)["bookable"] is True          # trusted default
    assert rc.receipt_to_journal(r, verified=True)["bookable"] is True
    j = rc.receipt_to_journal(r, verified=False)
    assert j["bookable"] is False
    assert "verifyReceiptFull" in j["exception_reason"]
    print("unverified receipt -> exception OK")


def test_exception_taxonomy_codes_and_actors():
    """CAAP-1 v1.1: exceptions carry Patrick's ReceiptFailure code + who-acts routing."""
    # delivery failed -> seller
    j = rc.receipt_to_journal(_receipt(delivery={"status": "failed"}))
    assert j["exception_code"] == "delivery_failed"
    assert j["exception_actor"] == "seller"
    # verification failed generic -> receipt_invalid, seller reissue
    j = rc.receipt_to_journal(_receipt(), verified=False)
    assert j["exception_code"] == "receipt_invalid"
    assert j["exception_actor"] == "seller"
    # explicit settlement_missing -> buyer
    j = rc.receipt_to_journal(_receipt(), failure_code="settlement_missing")
    assert j["exception_code"] == "settlement_missing"
    assert j["exception_actor"] == "buyer"
    # tampered -> manual hard stop
    j = rc.receipt_to_journal(_receipt(), failure_code="receipt_tampered")
    assert j["exception_code"] == "receipt_tampered"
    assert j["exception_actor"] == "manual"
    assert "fraud" in j["exception_reason"].lower() or "HARD STOP" in j["exception_reason"]
    print("exception taxonomy codes + actors OK")


def test_clean_receipt_has_no_exception_code():
    j = rc.receipt_to_journal(_receipt())
    assert j["bookable"] is True
    assert j["exception_code"] is None
    assert j["exception_actor"] is None
    print("clean receipt no exception code OK")


def test_failure_code_overrides_clean_delivery():
    """Even a delivered/200 receipt is an exception if verification found tampering."""
    j = rc.receipt_to_journal(_receipt(), failure_code="receipt_tampered")
    assert j["bookable"] is False
    print("failure_code overrides clean delivery OK")
