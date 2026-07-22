"""
Tests for buildercodes.py — builder-code evidence, conflicts, clustering.
Uses a temporary ledger so tests never touch the real out/counterra.db.
"""
import os
import sys
import tempfile
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from counterralib.ingest import PaymentEvent
from counterralib.store import EventStore
from counterralib import buildercodes as bc


def _ev(n, payee, app_code="", amount=1.0):
    e = PaymentEvent(
        tx_hash=f"0x{n:064x}",
        ts=datetime(2026, 7, 21, 12, 0, tzinfo=timezone.utc),
        chain="base",
        payer_wallet=f"0xPAYER{n}",
        payee_wallet=payee,
        amount_usdc=amount,
        protocol="x402",
        memo="",
    )
    e.app_code = app_code
    return e


def _db_with(events):
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.unlink(path)
    s = EventStore(path)
    s.add_events(events)
    s.close()
    return path


def test_codes_for_wallet():
    db = _db_with([
        _ev(1, "0xAAA", "bc_alpha"),
        _ev(2, "0xAAA", "bc_alpha"),
        _ev(3, "0xAAA", ""),          # no code
        _ev(4, "0xBBB", "bc_beta"),
    ])
    codes = bc.codes_for_wallet("0xaaa", db)
    assert codes == {"bc_alpha": 2}, codes
    assert bc.codes_for_wallet("0xCCC", db) == {}
    os.unlink(db)
    print("codes_for_wallet OK")


def test_evidence_string():
    db = _db_with([_ev(1, "0xAAA", "bc_alpha"), _ev(2, "0xAAA", "bc_alpha")])
    ev = bc.builder_code_evidence("0xAAA", db)
    assert "bc_alpha" in ev and "2 settlements" in ev, ev
    assert bc.builder_code_evidence("0xZZZ", db) is None
    os.unlink(db)
    print("evidence string OK")


def test_wallet_clustering_same_code():
    # One operator settling to two wallets under the same builder code
    db = _db_with([
        _ev(1, "0xAAA", "bc_shared"),
        _ev(2, "0xBBB", "bc_shared"),
    ])
    wallets = bc.wallets_for_code("bc_shared", db)
    assert set(wallets) == {"0xaaa", "0xbbb"}, wallets
    os.unlink(db)
    print("wallet clustering OK")


def test_detect_shared_code_conflict():
    db = _db_with([
        _ev(1, "0xAAA", "bc_shared"),
        _ev(2, "0xBBB", "bc_shared"),
    ])
    providers = {"0xAAA": {"label": "alpha.com"}, "0xBBB": {"label": "beta.com"}}
    conflicts = bc.detect_conflicts(providers, db)
    shared = [c for c in conflicts if c["type"] == "shared_code"]
    assert len(shared) == 1
    assert set(shared[0]["registry_labels"]) == {"alpha.com", "beta.com"}
    os.unlink(db)
    print("shared-code conflict detection OK")


def test_detect_multi_code_conflict():
    db = _db_with([
        _ev(1, "0xAAA", "bc_one"),
        _ev(2, "0xAAA", "bc_two"),
    ])
    conflicts = bc.detect_conflicts({"0xAAA": {"label": "alpha.com"}}, db)
    multi = [c for c in conflicts if c["type"] == "multi_code"]
    assert len(multi) == 1 and set(multi[0]["codes"]) == {"bc_one", "bc_two"}
    os.unlink(db)
    print("multi-code conflict detection OK")


def test_no_conflicts_when_clean():
    db = _db_with([_ev(1, "0xAAA", "bc_alpha"), _ev(2, "0xAAA", "bc_alpha")])
    assert bc.detect_conflicts({"0xAAA": {"label": "alpha.com"}}, db) == []
    os.unlink(db)
    print("clean registry -> no conflicts OK")


def test_unregistered_with_codes_ranked_by_value():
    db = _db_with([
        _ev(1, "0xKNOWN", "bc_known", amount=100.0),
        _ev(2, "0xNEW1", "bc_new1", amount=5.0),
        _ev(3, "0xNEW2", "bc_new2", amount=50.0),
        _ev(4, "0xNOCODE", "", amount=999.0),   # no code -> not a target
    ])
    providers = [{"wallet": "0xKNOWN", "label": "known.com"}]
    found = bc.unregistered_with_codes(providers, db)
    wallets = [f["wallet"] for f in found]
    assert "0xknown" not in wallets, "registered wallet must be excluded"
    assert "0xnocode" not in wallets, "wallet without a code is not a target"
    assert wallets == ["0xnew2", "0xnew1"], f"must rank by amount: {wallets}"
    assert found[0]["evidence"] and "bc_new2" in found[0]["evidence"]
    os.unlink(db)
    print("unregistered-with-codes ranking OK")


def test_missing_ledger_is_graceful():
    # No db file at all — must return empties, never raise
    assert bc.codes_for_wallet("0xAAA", "/tmp/does_not_exist_12345.db") == {}
    assert bc.builder_code_evidence("0xAAA", "/tmp/does_not_exist_12345.db") is None
    assert bc.detect_conflicts({}, "/tmp/does_not_exist_12345.db") == []
    assert bc.unregistered_with_codes([], "/tmp/does_not_exist_12345.db") == []
    print("missing ledger graceful OK")


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
    print("\nALL BUILDER-CODE TESTS PASSED")
