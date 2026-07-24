"""
Tests for observed.py — demand-side registry evidence.

Covers the two things most likely to go wrong: the sybil-aware ranking
(distinct payers must beat raw settlement count) and the framing rule
(unobserved sellers get None, never a negative string or a zeroed record).
"""
import os
import sys
import tempfile
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from counterralib.ingest import PaymentEvent
from counterralib.store import EventStore
from counterralib import observed as ob


def _ev(n, payee, payer, day=21, amount=1.0):
    return PaymentEvent(
        tx_hash="0x%064x" % n,
        ts=datetime(2026, 7, day, 12, 0, tzinfo=timezone.utc),
        chain="base",
        payer_wallet=payer,
        payee_wallet=payee,
        amount_usdc=amount,
        protocol="x402",
        memo="",
    )


def _db(events):
    fd, p = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.unlink(p)
    s = EventStore(p)
    s.add_events(events)
    s.close()
    return p


def test_observed_stats_basic():
    db = _db([
        _ev(1, "0xSELLER", "0xA", day=21, amount=2.0),
        _ev(2, "0xSELLER", "0xB", day=22, amount=3.0),
        _ev(3, "0xSELLER", "0xA", day=22, amount=1.0),
    ])
    s = ob.observed_stats("0xseller", db)
    assert s["settlements"] == 3, s
    assert s["distinct_payers"] == 2, s
    assert abs(s["amount_usdc"] - 6.0) < 1e-9, s
    assert s["days_active"] == 2, s
    os.unlink(db)
    print("observed_stats basic OK")


def test_unobserved_returns_none_not_zero():
    """FRAMING RULE: never a negative record for an unseen seller."""
    db = _db([_ev(1, "0xSELLER", "0xA")])
    assert ob.observed_stats("0xNOBODY", db) is None
    assert ob.observed_evidence("0xNOBODY", db) is None
    os.unlink(db)
    print("unobserved -> None (no zeroed/negative record) OK")


def test_evidence_string_mentions_distinct_payers():
    db = _db([_ev(1, "0xS", "0xA"), _ev(2, "0xS", "0xB")])
    ev = ob.observed_evidence("0xS", db)
    assert "2 distinct payers" in ev, ev
    assert "2 settlements" in ev, ev
    os.unlink(db)
    print("evidence string OK")


def test_ranking_prefers_distinct_payers_over_raw_count():
    """Sybil lesson: many settlements from one payer must rank BELOW
    fewer settlements from many payers."""
    flood = [_ev(i, "0xFLOOD", "0xSAME", amount=10.0) for i in range(1, 21)]  # 20 from 1
    spread = [_ev(100 + i, "0xSPREAD", "0xP%d" % i, amount=0.01) for i in range(5)]  # 5 from 5
    db = _db(flood + spread)
    rows = ob.annotate_registry([{"wallet": "0xFLOOD"}, {"wallet": "0xSPREAD"}], db)
    assert rows[0]["wallet"] == "0xSPREAD", [r["wallet"] for r in rows]
    assert rows[0]["observed"]["distinct_payers"] == 5
    assert rows[1]["observed"]["settlements"] == 20  # bigger count, still ranked lower
    os.unlink(db)
    print("ranking: distinct payers beat raw count OK")


def test_annotate_registry_includes_unobserved():
    db = _db([_ev(1, "0xSEEN", "0xA")])
    rows = ob.annotate_registry([{"wallet": "0xSEEN", "label": "seen.com"},
                                 {"wallet": "0xUNSEEN", "label": "unseen.com"}], db)
    assert len(rows) == 2
    seen = [r for r in rows if r["wallet"] == "0xSEEN"][0]
    unseen = [r for r in rows if r["wallet"] == "0xUNSEEN"][0]
    assert seen["observed"] is not None and seen["evidence"]
    assert unseen["observed"] is None and unseen["evidence"] is None
    assert rows[0]["wallet"] == "0xSEEN"  # observed ranks above unobserved
    os.unlink(db)
    print("annotate_registry includes unobserved without penalising OK")


def test_annotate_accepts_dict_registry():
    db = _db([_ev(1, "0xS", "0xA")])
    rows = ob.annotate_registry({"0xS": {"label": "s.com", "category": "Market data"}}, db)
    assert rows[0]["label"] == "s.com" and rows[0]["category"] == "Market data"
    os.unlink(db)
    print("dict-shaped registry accepted OK")


def test_ledger_window_reports_coverage():
    db = _db([_ev(1, "0xS", "0xA", day=21), _ev(2, "0xS", "0xB", day=24)])
    w = ob.ledger_window(db)
    assert w["events"] == 2 and w["days"] == 4, w
    os.unlink(db)
    print("ledger_window coverage OK")


def test_demand_concentration():
    db = _db([_ev(i, "0xBIG", "0xP%d" % i) for i in range(1, 10)] +
             [_ev(100, "0xSMALL", "0xX")])
    c = ob.demand_concentration(db)
    assert c["payees"] == 2 and c["total_settlements"] == 10
    assert c["top_payee"] == "0xbig"
    assert abs(c["top_share_settlements"] - 0.9) < 1e-6, c
    os.unlink(db)
    print("demand concentration OK")


def test_empty_and_missing_ledger_graceful():
    missing = "/tmp/definitely_not_a_db_98765.db"
    assert ob.observed_stats("0xS", missing) is None
    assert ob.observed_evidence("0xS", missing) is None
    assert ob.ledger_window(missing) is None
    assert ob.demand_concentration(missing) is None
    assert ob.annotate_registry([{"wallet": "0xS"}], missing)[0]["observed"] is None
    print("missing ledger graceful OK")


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
    print("\nALL OBSERVED-EVIDENCE TESTS PASSED")
