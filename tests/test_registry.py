"""
Tests for registry.py - the registry/pending-queue plumbing behind autogrow.

Covers: dedup on append, the pending-queue lifecycle (queue -> approve/reject),
that approving strips the confidence field before publishing, and that
unknown_sellers ranks strictly by dollar volume and respects the known-set.
All in-memory / tmp files - no network.
"""
import datetime
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from counterralib import registry as rg
from counterralib.ingest import PaymentEvent


def _ev(payee, amt, chain="base", payer="0x" + "1" * 40):
    return PaymentEvent(
        tx_hash="0x" + os.urandom(8).hex(), ts=datetime.datetime(2026, 8, 1),
        chain=chain, payer_wallet=payer, payee_wallet=payee,
        amount_usdc=amt, protocol="x402", memo="")


def test_append_verified_dedupes():
    reg = {"providers": [
        {"wallet": "0xaaa", "chain": "base", "label": "old", "category": "X",
         "evidence": "e", "added": "2026-01-01"}]}
    e1 = rg.make_entry("0xAAA", "base", "dup", "Y", "ev")     # dup (case)
    e2 = rg.make_entry("0xbbb", "base", "new", "Y", "ev")
    added = rg.append_verified(reg, [e1, e2])
    assert len(added) == 1 and added[0]["wallet"] == "0xbbb", added
    assert len(reg["providers"]) == 2
    print("append_verified dedup OK")


def test_pending_lifecycle():
    pend = rg.load_pending(path="/nonexistent/nope.json")  # fresh structure
    reg = {"providers": []}
    known = set()
    e = rg.make_entry("0xccc", "base", "maybe.ai", "AI inference", "tag ev")
    assert rg.queue_probable(pend, e, "probable", known)
    assert not rg.queue_probable(pend, e, "probable", known)   # no double-queue
    assert pend["pending"][0]["confidence"] == "probable"
    # approving publishes it, stripped of the confidence field
    out = rg.approve_pending(pend, reg, 1)
    assert out and out["wallet"] == "0xccc"
    assert "confidence" not in reg["providers"][0]
    assert pend["pending"] == []
    # reject path
    e2 = rg.make_entry("0xddd", "base", "nah", "X", "ev")
    rg.queue_probable(pend, e2, "probable", rg.known_wallets(reg))
    gone = rg.reject_pending(pend, 1)
    assert gone["wallet"] == "0xddd" and pend["pending"] == []
    print("pending queue lifecycle OK")


def test_queue_probable_skips_known():
    pend = {"pending": []}
    e = rg.make_entry("0xeee", "base", "known.ai", "X", "ev")
    assert not rg.queue_probable(pend, e, "probable", {"0xeee"})
    assert pend["pending"] == []
    print("queue skips already-known OK")


def test_unknown_sellers_volume_ranked():
    events = [_ev("0xS1", 1.0), _ev("0xS1", 1.0),          # $2 total
              _ev("0xS2", 5.0),                            # $5 - should rank 1st
              _ev("0xS3", 0.5),
              _ev("0xKNOWN", 100.0)]                       # mapped: excluded
    out = rg.unknown_sellers(events, known={"0xknown"})
    assert [w for w, _, _, _ in out] == ["0xs2", "0xs1", "0xs3"], out
    assert out[0][2] == 5.0 and out[1][3] == 2               # totals + counts
    print("unknown_sellers volume ranking OK")


def test_unknown_sellers_chain_filter():
    events = [_ev("0xS1", 1.0, chain="base"),
              _ev("SoLAddr11111111111111111111111111111111111", 9.0,
                  chain="solana")]
    out = rg.unknown_sellers(events, known=set(), chain="base")
    assert len(out) == 1 and out[0][1] == "base"
    print("unknown_sellers chain filter OK")


def test_registry_roundtrip_tmpfile():
    reg = {"version": 1, "providers": []}
    rg.append_verified(reg, [rg.make_entry("0xfff", "base", "x", "y", "z")])
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
        p = f.name
    try:
        rg.save_registry(reg, path=p)
        back = rg.load_registry(path=p)
        assert back["providers"][0]["wallet"] == "0xfff"
        assert back["providers"][0]["added"] == datetime.date.today().isoformat()
    finally:
        os.unlink(p)
    print("registry save/load roundtrip OK")


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
    print("\nALL REGISTRY TESTS PASSED")
