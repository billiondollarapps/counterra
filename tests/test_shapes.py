"""
Tests for shapes.py - Stage 5 shape-based categorisation.

The load-bearing properties, in order of how much damage getting them wrong
would do:
  1. A real registry identification ALWAYS beats a behavioural guess.
  2. Low-evidence wallets are left alone, not guessed at.
  3. Shape categories are visibly distinct from identified ones (the
     "Unidentified - " prefix) and stay in the exception queue.
  4. Each shape is inferred from the pattern it claims to describe.
No network; synthetic events only.
"""
import datetime
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from counterralib import shapes as sh
from counterralib.ingest import PaymentEvent
from counterralib.ledger import enrich, grouped_exceptions

T0 = datetime.datetime(2026, 8, 1, 0, 0, 0)


def _ev(payee, amt, secs, payer="0x" + "1" * 40):
    return PaymentEvent(
        tx_hash="0x" + os.urandom(6).hex(), ts=T0 + datetime.timedelta(seconds=secs),
        chain="base", payer_wallet=payer, payee_wallet=payee,
        amount_usdc=amt, protocol="x402", memo="")


def test_metered_api_shape():
    # 60 flat sub-cent settlements, seconds apart - the 545-settlement case.
    evs = [_ev("0xAPI", 0.002, i * 30) for i in range(60)]
    out = sh.classify_shape(evs)
    assert out["category"] == sh.CAT_METERED, out
    assert out["confidence"] == "high", out
    assert "machine cadence" in out["rationale"]
    print("metered API shape OK")


def test_tiered_service_shape():
    evs = ([_ev("0xT", 0.05, i * 60) for i in range(5)]
           + [_ev("0xT", 1.0, 500 + i * 60) for i in range(4)]
           + [_ev("0xT", 5.0, 900 + i * 60) for i in range(3)])
    out = sh.classify_shape(evs)
    assert out["category"] == sh.CAT_TIERED, out
    assert "3 distinct price points" in out["rationale"]
    print("tiered service shape OK")


def test_recurring_charge_shape():
    # identical amount, once a day
    evs = [_ev("0xR", 0.25, i * 86400) for i in range(10)]
    out = sh.classify_shape(evs)
    assert out["category"] == sh.CAT_RECURRING, out
    assert "24.0h cadence" in out["rationale"]
    print("recurring charge shape OK")


def test_bulk_purchase_shape():
    evs = [_ev("0xB", 25.0, 0), _ev("0xB", 40.0, 90000),
           _ev("0xB", 12.5, 200000), _ev("0xB", 30.0, 400000),
           _ev("0xB", 18.0, 700000), _ev("0xB", 22.0, 900000)]
    out = sh.classify_shape(evs)
    assert out["category"] == sh.CAT_BULK, out
    assert out["confidence"] == "medium"
    print("bulk purchase shape OK")


def test_low_evidence_refused():
    # Below MIN_SETTLEMENTS: say nothing rather than guess.
    assert sh.classify_shape([_ev("0xX", 0.01, 0)]) is None
    assert sh.classify_shape([_ev("0xX", 0.01, i * 10) for i in range(3)]) is None
    print("low-evidence wallets left alone OK")


def test_identification_beats_shape():
    evs = [_ev("0xKNOWN", 0.002, i * 30) for i in range(60)]
    m = sh.shape_map(evs, known_wallets=["0xknown"])
    assert m == {}, m
    # and through enrich: registry category survives even if a shape existed
    rows = enrich(evs, {}, {"0xknown": {"label": "RealSeller",
                                        "category": "AI inference"}},
                  shape_map={"0xknown": {"category": sh.CAT_METERED,
                                         "label": "Unidentified metered API",
                                         "confidence": "high",
                                         "rationale": "x"}})
    assert rows[0]["category"] == "AI inference"
    assert rows[0]["provider"] == "RealSeller"
    print("real identification beats shape OK")


def test_enrich_applies_shape_and_marks_it():
    evs = [_ev("0xAPI", 0.002, i * 30) for i in range(60)]
    m = sh.shape_map(evs)
    rows = enrich(evs, {}, {}, shape_map=m)
    assert rows[0]["category"] == sh.CAT_METERED
    assert rows[0]["provider"].startswith("Unidentified metered API")
    assert rows[0]["shape_confidence"] == "high"
    assert rows[0]["shape_rationale"]
    print("enrich applies shape + provenance fields OK")


def test_enrich_without_shape_map_unchanged():
    evs = [_ev("0xAPI", 0.002, i * 30) for i in range(5)]
    rows = enrich(evs, {}, {})
    assert rows[0]["category"] == "Uncategorized"
    assert rows[0]["provider"].endswith("\u2026")
    print("enrich unchanged when no shape_map OK")


def test_shaped_rows_stay_in_exception_queue():
    # Shape-classified spend must NOT vanish from review - it is still
    # unidentified, just better described.
    evs = [_ev("0xAPI", 0.002, i * 30) for i in range(60)]
    rows = enrich(evs, {}, {}, shape_map=sh.shape_map(evs))
    g = grouped_exceptions(rows)
    assert len(g) == 1, g
    assert "name still unknown" in g[0]["reason"], g[0]["reason"]
    assert "metered API" in g[0]["reason"]
    print("shaped spend stays reviewable OK")


def test_shape_categories_are_distinguishable():
    for c in sh.SHAPE_CATEGORIES:
        assert sh.is_shape_category(c)
    assert not sh.is_shape_category("AI inference")
    assert not sh.is_shape_category("Uncategorized")
    assert not sh.is_shape_category(None)
    print("shape categories distinguishable from identified ones OK")


def test_shape_accounts_are_separate_and_overridable():
    merged = sh.merged_expense_accounts({"Uncategorized": "6490 - x"})
    assert merged[sh.CAT_METERED].startswith("6480")
    assert merged["Uncategorized"] == "6490 - x"
    # config wins over the default
    over = sh.merged_expense_accounts({sh.CAT_METERED: "9999 - mine"})
    assert over[sh.CAT_METERED] == "9999 - mine"
    print("shape accounts separate + config-overridable OK")


def test_coverage_math():
    evs = ([_ev("0xKNOWN", 1.0, 0)]                              # identified
           + [_ev("0xAPI", 0.01, i * 30) for i in range(10)]     # shaped: $0.10
           + [_ev("0xMYSTERY", 0.90, 0)])                        # unknown
    m = sh.shape_map(evs, known_wallets=["0xknown"])
    cov = sh.coverage(evs, m, known_wallets=["0xknown"])
    assert cov["identified_usd"] == 1.0
    assert round(cov["shaped_usd"], 2) == 0.10
    assert cov["unknown_usd"] == 0.90
    assert cov["booked_pct"] == 55.0, cov          # 1.10 of 2.00
    print("coverage math OK")


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
    print("\nALL SHAPE TESTS PASSED")
