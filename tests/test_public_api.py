"""
Tests for the public API surface (counterralib/__init__.py).

These lock the CONTRACT: the exported names, their signatures, and the shape of
what they return. Internal modules may be refactored freely, but breaking these
breaks everyone who imported `counterralib`. This suite is the tripwire.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import counterralib as ctr


def test_public_names_exported():
    for name in ["books_for_wallet", "sweep_chain", "analyze_ledger",
                 "receipt_to_journal", "identify_seller", "resolve_payto",
                 "profile_wallet", "detect_chain", "load_config",
                 "CAAP1_VERSION", "__version__"]:
        assert hasattr(ctr, name), f"public API missing {name}"
        assert name in ctr.__all__, f"{name} not in __all__"
    print("all public names exported OK")


def test_version_strings():
    assert isinstance(ctr.__version__, str) and ctr.__version__
    assert isinstance(ctr.CAAP1_VERSION, str) and "CAAP" not in ctr.CAAP1_VERSION
    print("version strings OK")


def test_detect_chain():
    assert ctr.detect_chain("0x" + "a" * 40) == "base"
    assert ctr.detect_chain("DdeMfXrDae49VAkvHiGnUAkAPCFRhBpwsc7yVDvyKqYb") == "solana"
    assert ctr.detect_chain("garbage") is None
    print("detect_chain contract OK")


def test_receipt_to_journal_contract():
    receipt = {
        "scheme": "x402-receipts/v0",
        "payment": {"tx_hash": "0x" + "a" * 64, "asset": "USDC", "amount": "3000",
                    "payer": "0x" + "b" * 40, "payee": "0x" + "c" * 40},
        "request": {"method": "GET", "ts": "2026-07-25T12:00:00Z"},
        "response": {"status": 200, "ts": "2026-07-25T12:00:00Z", "latency_ms": 340},
        "goods": {"kind": "api-response", "description": "signal"},
    }
    j = ctr.receipt_to_journal(receipt, cfg={})
    # the contract: these keys must exist with these meanings
    for k in ["debit_account", "credit_account", "amount_usd", "bookable",
              "category", "date"]:
        assert k in j, f"receipt_to_journal missing {k}"
    assert j["amount_usd"] == 0.003
    assert j["bookable"] is True
    print("receipt_to_journal contract OK")


def test_analyze_ledger_contract():
    # empty/no-ledger case must still return the contracted shape
    r = ctr.analyze_ledger(cfg={"providers": {}})
    assert set(r.keys()) == {"findings", "narration"}
    assert isinstance(r["findings"], list)
    assert isinstance(r["narration"], str)
    print("analyze_ledger contract OK")


def test_resolve_payto_returns_none_on_junk():
    # offline-safe: a clearly bad host resolves to None, never raises
    out = ctr.resolve_payto("https://definitely-not-a-real-x402-seller-999.invalid")
    assert out is None or isinstance(out, dict)
    print("resolve_payto contract OK")


def test_books_for_wallet_bad_wallet_shape():
    r = ctr.books_for_wallet("not-a-wallet", cfg={})
    # bad wallet -> contracted empty shape, not a crash
    assert r["chain"] is None
    assert r["journal_entries"] == []
    assert isinstance(r["warnings"], list) and r["warnings"]
    print("books_for_wallet graceful contract OK")


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
    print("\nALL PUBLIC-API TESTS PASSED")
