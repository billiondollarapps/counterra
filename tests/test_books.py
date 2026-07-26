"""
Tests for books.py — the one-command design-partner handoff.

Focus: chain auto-detection (so a partner never passes a flag), and graceful
handling of empty results and bad input (so the handoff never crashes in front
of someone who isn't the author).
"""
import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from counterralib import books as bk


def test_detect_base_address():
    assert bk.detect_chain("0x" + "a" * 40) == "base"
    assert bk.detect_chain("0xABCdef0123456789012345678901234567890123") == "base"


def test_detect_solana_address():
    assert bk.detect_chain("DdeMfXrDae49VAkvHiGnUAkAPCFRhBpwsc7yVDvyKqYb") == "solana"
    assert bk.detect_chain("8mTdwZCZDPeDhso3") is None  # too short, not trusted


def test_detect_rejects_junk():
    assert bk.detect_chain("") is None
    assert bk.detect_chain("hello") is None
    assert bk.detect_chain("0xtooshort") is None
    assert bk.detect_chain("0x" + "z" * 40) is None  # not hex
    print("chain detection OK")


def test_build_books_bad_address_returns_warning():
    events, chain, warnings = bk.build_books("not-an-address", {})
    assert events == [] and chain is None
    assert warnings and "recognise" in warnings[0]
    print("bad address handled gracefully OK")


def test_partner_summary_empty():
    assert "No x402 spend" in bk.partner_summary([], "base")
    print("empty summary OK")


def test_partner_summary_with_events():
    evs = [SimpleNamespace(amount_usdc=1.5, payee_wallet="0xAAA"),
           SimpleNamespace(amount_usdc=0.5, payee_wallet="0xBBB"),
           SimpleNamespace(amount_usdc=0.25, payee_wallet="0xAAA")]
    s = bk.partner_summary(evs, "base")
    assert "3 payments" in s
    assert "$2.2500" in s
    assert "2 seller(s)" in s
    print("populated summary OK")


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
    print("\nALL BOOKS TESTS PASSED")


def test_load_receipts_from_folder():
    import json, tempfile, os
    d = tempfile.mkdtemp()
    r = {"payment": {"tx_hash": "0xABC123"}, "goods": {"description": "signal"}}
    json.dump(r, open(os.path.join(d, "r1.json"), "w"))
    json.dump([{"payment": {"tx_hash": "0xDEF456"}}], open(os.path.join(d, "r2.json"), "w"))
    open(os.path.join(d, "junk.json"), "w").write("not json{{{")
    loaded = bk.load_receipts(d)
    assert "0xabc123" in loaded and "0xdef456" in loaded
    assert len(loaded) == 2  # junk skipped
    print("load_receipts OK")


def test_enrich_events_with_receipts():
    from types import SimpleNamespace
    events = [SimpleNamespace(tx_hash="0xABC", memo="facilitator-settled"),
              SimpleNamespace(tx_hash="0xZZZ", memo="facilitator-settled")]
    receipts = {"0xabc": {"request": {"method": "GET"},
                          "goods": {"description": "BTC signal"},
                          "delivery": {"status": "delivered"},
                          "response": {"status": 200}}}
    events, matched = bk.enrich_events_with_receipts(events, receipts)
    assert matched == 1
    assert "BTC signal" in events[0].memo and "delivered" in events[0].memo
    assert events[1].memo == "facilitator-settled"  # unmatched untouched
    print("enrich_events_with_receipts OK")


def test_enrich_with_no_receipts_is_noop():
    from types import SimpleNamespace
    events = [SimpleNamespace(tx_hash="0xABC", memo="x")]
    events, matched = bk.enrich_events_with_receipts(events, {})
    assert matched == 0 and events[0].memo == "x"
    print("no-receipts noop OK")
