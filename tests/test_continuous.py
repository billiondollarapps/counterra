"""
Tests for continuous ingestion (store.py + continuous.py).

Proves the core promise: run twice, books accumulate; re-run an overlapping
window, nothing double-counts.
"""
import os
import sys
import tempfile
from datetime import datetime, timezone, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from counterralib.ingest import PaymentEvent
from counterralib.store import EventStore
from counterralib.continuous import ingest_run


def _ev(n, day=1, chain="base", amount=1.0, payer="0xAGENT", payee="0xSELLER"):
    return PaymentEvent(
        tx_hash=f"0x{n:064x}",
        ts=datetime(2026, 7, day, 12, 0, tzinfo=timezone.utc),
        chain=chain,
        payer_wallet=payer,
        payee_wallet=payee,
        amount_usdc=amount,
        protocol="x402",
        memo=f"event {n}",
    )


def _tmp_db():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.unlink(path)  # let EventStore create it fresh
    return path


def test_store_add_and_retrieve():
    db = _tmp_db()
    s = EventStore(db)
    n = s.add_events([_ev(1), _ev(2), _ev(3)])
    assert n == 3, n
    assert s.event_count() == 3
    evs = s.all_events()
    assert len(evs) == 3
    assert evs[0].memo == "event 1"
    s.close()
    os.unlink(db)
    print("store add+retrieve OK")


def test_idempotent_no_double_count():
    db = _tmp_db()
    s = EventStore(db)
    s.add_events([_ev(1), _ev(2)])
    # Re-add the same two plus one new — only the new one should land
    n = s.add_events([_ev(1), _ev(2), _ev(3)])
    assert n == 1, f"expected 1 new, got {n}"
    assert s.event_count() == 3
    s.close()
    os.unlink(db)
    print("idempotent re-ingest OK")


def test_accumulates_across_runs():
    db = _tmp_db()
    # Run 1: Monday, 2 events
    all1, stats1 = ingest_run([_ev(1, day=1), _ev(2, day=1)],
                              chain="base", source="sweep", db_path=db)
    assert stats1["new_this_run"] == 2
    assert stats1["total_events"] == 2
    assert stats1["prior_watermark"] is None

    # Run 2: Wednesday, 2 new events + 1 repeat from Monday
    all2, stats2 = ingest_run([_ev(2, day=1), _ev(3, day=3), _ev(4, day=3)],
                              chain="base", source="sweep", db_path=db)
    assert stats2["new_this_run"] == 2, stats2  # only 3 and 4 are new
    assert stats2["total_events"] == 4, stats2  # accumulated, not reset
    assert stats2["prior_watermark"] is not None
    # Watermark advanced to the later day
    assert stats2["new_watermark"] > stats1["new_watermark"]
    os.unlink(db)
    print("accumulates across runs OK (2 then +2 = 4, not reset to 2)")


def test_chains_are_isolated():
    db = _tmp_db()
    ingest_run([_ev(1, chain="base"), _ev(2, chain="base")],
               chain="base", source="sweep", db_path=db)
    ingest_run([_ev(3, chain="solana")],
               chain="solana", source="sweep", db_path=db)
    s = EventStore(db)
    assert s.event_count("base") == 2
    assert s.event_count("solana") == 1
    assert s.event_count() == 3
    assert all(e.chain == "base" for e in s.all_events("base"))
    s.close()
    os.unlink(db)
    print("chain isolation OK")


def test_watermark_never_goes_backward():
    db = _tmp_db()
    s = EventStore(db)
    s.set_watermark("base", "sweep", "2026-07-10T00:00:00+00:00")
    s.set_watermark("base", "sweep", "2026-07-05T00:00:00+00:00")  # older
    assert s.get_watermark("base", "sweep") == "2026-07-10T00:00:00+00:00"
    s.close()
    os.unlink(db)
    print("watermark monotonic OK")


def test_events_survive_reopen():
    db = _tmp_db()
    s1 = EventStore(db)
    s1.add_events([_ev(1), _ev(2)])
    s1.close()
    # Reopen — persistence is the whole point
    s2 = EventStore(db)
    assert s2.event_count() == 2
    s2.close()
    os.unlink(db)
    print("persistence across reopen OK")


def test_attribution_fields_round_trip():
    db = _tmp_db()
    e = _ev(1)
    e.app_code = "counterra"
    e.facilitator_code = "cdp_fac"
    e.service_codes = "agent_x"
    e.attribution_evidence = "0xdeadbeef"
    s = EventStore(db)
    s.add_events([e])
    back = s.all_events()[0]
    assert back.app_code == "counterra"
    assert back.facilitator_code == "cdp_fac"
    assert back.service_codes == "agent_x"
    assert back.attribution_evidence == "0xdeadbeef"
    s.close()
    os.unlink(db)
    print("ERC-8021 fields round-trip OK")


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
    print("\nALL CONTINUOUS-INGESTION TESTS PASSED")
