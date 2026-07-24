"""
Tests for profile.py — settlement-pattern fingerprinting of unknown sellers.

The important guarantees: price ladders and tier counts are computed
correctly, payer overlap only counts OTHER sellers, exclusivity is measured
against the visible ledger, and unidentified wallets rank by book impact.
"""
import os
import sys
import tempfile
from datetime import datetime, timezone, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from counterralib.ingest import PaymentEvent
from counterralib.store import EventStore
from counterralib import profile as pf


def _ev(n, payee, payer, amount, minutes=0, app_code=""):
    e = PaymentEvent(
        tx_hash="0x%064x" % n,
        ts=datetime(2026, 7, 21, 12, 0, tzinfo=timezone.utc) + timedelta(minutes=minutes),
        chain="base",
        payer_wallet=payer,
        payee_wallet=payee,
        amount_usdc=amount,
        protocol="x402",
        memo="facilitator-settled",
    )
    e.app_code = app_code
    return e


def _db(events):
    fd, p = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.unlink(p)
    s = EventStore(p)
    s.add_events(events)
    s.close()
    return p


def test_price_ladder_and_tier_count():
    db = _db([
        _ev(1, "0xUNK", "0xA", 0.003), _ev(2, "0xUNK", "0xB", 0.003),
        _ev(3, "0xUNK", "0xC", 1.0), _ev(4, "0xUNK", "0xD", 15.0),
    ])
    p = pf.profile_wallet("0xUNK", {}, db)
    assert p["tier_count"] == 3, p["price_ladder"]
    assert p["max_price"] == 15.0 and p["min_price"] == 0.003
    assert p["price_ladder"][0] == (0.003, 2)  # most frequent first
    os.unlink(db)
    print("price ladder + tier count OK")


def test_tiered_note_emitted():
    db = _db([_ev(i, "0xUNK", "0x%d" % i, amt)
              for i, amt in enumerate([0.003, 1.0, 5.0, 15.0], start=1)])
    p = pf.profile_wallet("0xUNK", {}, db)
    joined = " ".join(p["notes"])
    assert "distinct price points" in joined, p["notes"]
    assert "far above the sub-cent norm" in joined, p["notes"]
    os.unlink(db)
    print("tiered/high-price notes OK")


def test_single_price_point_note():
    db = _db([_ev(1, "0xUNK", "0xA", 0.01), _ev(2, "0xUNK", "0xB", 0.01)])
    p = pf.profile_wallet("0xUNK", {}, db)
    assert any("single price point" in n for n in p["notes"]), p["notes"]
    os.unlink(db)
    print("single-price note OK")


def test_payer_overlap_counts_only_other_sellers():
    db = _db([
        _ev(1, "0xUNK", "0xA", 1.0),
        _ev(2, "0xUNK", "0xA", 1.0),      # same seller: must NOT count as overlap
        _ev(3, "0xKNOWN", "0xA", 0.5),    # same payer buying elsewhere: overlap
    ])
    providers = {"0xKNOWN": {"label": "known.com"}}
    p = pf.profile_wallet("0xUNK", providers, db)
    assert p["payer_overlap"] == [("known.com", 1)], p["payer_overlap"]
    os.unlink(db)
    print("payer overlap excludes self OK")


def test_exclusivity_when_payers_buy_nowhere_else():
    db = _db([_ev(i, "0xUNK", "0xP%d" % i, 1.0) for i in range(1, 5)])
    p = pf.profile_wallet("0xUNK", {}, db)
    assert p["exclusivity"] == 1.0, p["exclusivity"]
    assert any("no other seller" in n for n in p["notes"]), p["notes"]
    os.unlink(db)
    print("full exclusivity detected OK")


def test_exclusivity_partial():
    db = _db([
        _ev(1, "0xUNK", "0xA", 1.0), _ev(2, "0xUNK", "0xB", 1.0),
        _ev(3, "0xOTHER", "0xA", 1.0),   # 0xA also buys elsewhere
    ])
    p = pf.profile_wallet("0xUNK", {}, db)
    assert p["exclusive_payers"] == 1 and p["distinct_payers"] == 2
    assert abs(p["exclusivity"] - 0.5) < 1e-9
    os.unlink(db)
    print("partial exclusivity OK")


def test_cadence():
    db = _db([_ev(1, "0xUNK", "0xA", 1.0, minutes=0),
              _ev(2, "0xUNK", "0xA", 1.0, minutes=10),
              _ev(3, "0xUNK", "0xA", 1.0, minutes=30)])
    p = pf.profile_wallet("0xUNK", {}, db)
    c = p["cadence"]
    assert c["min_gap_s"] == 600.0 and c["max_gap_s"] == 1200.0
    os.unlink(db)
    print("cadence gaps OK")


def test_cadence_none_for_single_settlement():
    db = _db([_ev(1, "0xUNK", "0xA", 1.0)])
    assert pf.profile_wallet("0xUNK", {}, db)["cadence"] is None
    os.unlink(db)
    print("cadence None for single settlement OK")


def test_builder_code_note_when_absent_and_present():
    db1 = _db([_ev(1, "0xUNK", "0xA", 1.0)])
    p1 = pf.profile_wallet("0xUNK", {}, db1)
    assert p1["builder_codes"] == []
    assert any("no ERC-8021 builder code" in n for n in p1["notes"])
    os.unlink(db1)

    db2 = _db([_ev(1, "0xUNK", "0xA", 1.0, app_code="bc_abc")])
    p2 = pf.profile_wallet("0xUNK", {}, db2)
    assert p2["builder_codes"] == ["bc_abc"]
    assert not any("no ERC-8021" in n for n in p2["notes"])
    os.unlink(db2)
    print("builder-code notes OK")


def test_unidentified_ranked_by_value():
    db = _db([
        _ev(1, "0xKNOWN", "0xA", 99.0),
        _ev(2, "0xBIG", "0xA", 42.0),
        _ev(3, "0xSMALL", "0xB", 0.01),
    ])
    providers = {"0xKNOWN": {"label": "known.com"}}
    ranked = pf.unidentified_ranked(providers, db)
    wallets = [r["wallet"].lower() for r in ranked]
    assert "0xknown" not in wallets, "registered wallet must be excluded"
    assert wallets[0] == "0xbig", wallets
    os.unlink(db)
    print("unidentified ranked by book impact OK")


def test_unseen_wallet_and_missing_db():
    db = _db([_ev(1, "0xUNK", "0xA", 1.0)])
    assert pf.profile_wallet("0xNOBODY", {}, db) is None
    os.unlink(db)
    missing = "/tmp/no_such_ledger_4242.db"
    assert pf.profile_wallet("0xUNK", {}, missing) is None
    assert pf.unidentified_ranked({}, missing) == []
    print("unseen wallet / missing db graceful OK")


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
    print("\nALL PROFILE TESTS PASSED")
