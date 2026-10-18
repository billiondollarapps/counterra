"""
Offline tests for the network label book (counterralib/labels.py).
Run:  python3 tests/test_labels.py
"""
import datetime
import os
import sqlite3
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from counterralib import labels as L
from counterralib.ingest import PaymentEvent
from counterralib.store import EventStore

T0 = datetime.datetime(2026, 10, 1, tzinfo=datetime.timezone.utc)

VENDOR = "0xa11ce00000000000000000000000000000000001"     # registry: verified
PENDING = "0xb0b0000000000000000000000000000000000002"    # pending: probable
JUNK = "0xd3843e1f73178b78942fe5ebaeb1ac30611f7734"       # pending, explorer host
METERED = "0xc0ffee0000000000000000000000000000000003"    # unnamed, metered shape
STRANGER = "0xd00d000000000000000000000000000000000004"   # 1 payment: unknown
CLONE = "0xa11ce0000000000000000000000000000ff00001"      # lookalike of VENDOR
ESCROW = "0x4020074e9df2ce1dee5a9c1b5c3f541d02a10003"
FAC = "0x2a89407a98a0732b7fd578c4e156b7166540eb5a"
AGENT = "0x1111000000000000000000000000000000000aa1"
AGENT2 = "0x2222000000000000000000000000000000000aa2"
LOOPER = "0x3333000000000000000000000000000000000aa3"
LOOPSELLER = "0xe55e000000000000000000000000000000000005"


def ev(i, payer, payee, amt, memo="x402:exact", chain="base", mins=1):
    return PaymentEvent(tx_hash="0x%064x" % i, ts=T0 + datetime.timedelta(minutes=i * mins),
                        chain=chain, payer_wallet=payer, payee_wallet=payee,
                        amount_usdc=amt, protocol="x402", memo=memo, facilitator="Coinbase")


def fixture():
    evs, i = [], 0
    for _ in range(6):                                   # agent pays vendor
        i += 1; evs.append(ev(i, AGENT, VENDOR, 0.5))
    for _ in range(3):
        i += 1; evs.append(ev(i, AGENT2, VENDOR, 0.5))
    i += 1; evs.append(ev(i, AGENT, PENDING, 0.2))
    i += 1; evs.append(ev(i, AGENT, JUNK, 0.3))
    for _ in range(30):                                   # metered API, 2 buyers
        i += 1; evs.append(ev(i, AGENT if i % 2 else AGENT2, METERED, 0.001))
    i += 1; evs.append(ev(i, AGENT2, STRANGER, 9.0))
    i += 1; evs.append(ev(i, AGENT, CLONE, 0.000001))     # poisoning dust
    i += 1; evs.append(ev(i, AGENT, AGENT, 1.0))          # self-payment
    i += 1; evs.append(ev(i, AGENT2, ESCROW, 5.0, memo="x402:batch-deposit"))
    i += 1; evs.append(ev(i, ESCROW, METERED, 2.0, memo="x402:batch-payout"))
    for _ in range(12):                                   # closed loop
        i += 1; evs.append(ev(i, LOOPER, LOOPSELLER, 0.25))
    # legacy row from before leg typing
    i += 1; evs.append(ev(i, AGENT2, VENDOR, 0.5, memo="facilitator-settled"))
    return evs


REGISTRY = {"providers": [{"wallet": VENDOR, "chain": "base", "label": "Vendor Co",
                           "category": "Market data", "evidence": "catalog payTo match"}]}
PENDING_Q = {"pending": [
    {"wallet": PENDING, "chain": "base", "label": "bob.example", "category": "AI inference",
     "evidence": "address-in-text", "confidence": "probable"},
    {"wallet": JUNK, "chain": "base", "label": "sepolia-explorer.giwa.io",
     "category": "Uncategorized", "evidence": "address-in-text", "confidence": "probable"},
]}
CENSUS = {"wallets": [{"chain": "base", "address": FAC, "name": "Coinbase",
                       "facilitator": "coinbase", "deprecated": False, "first_tx": "2026-06-15"},
                      {"chain": "polygon", "address": "0x9999999999999999999999999999999999999999",
                       "name": "Old", "facilitator": "old", "deprecated": True, "first_tx": None}]}


def main():
    evs = fixture()
    res = L.build(evs, registry=REGISTRY, pending=PENDING_Q, census=CENSUS,
                  accounting={"expense_accounts": {"Market data": "6410 - Data"}})
    W = res["wallets"]

    assert W[VENDOR]["tier"] == "verified" and W[VENDOR]["name"] == "Vendor Co"
    assert W[VENDOR]["as_seller"]["buyers"] == 2
    assert W[VENDOR]["as_seller"]["settlements"] == 10, "legacy memo counts as a payment"
    assert W[PENDING]["tier"] == "probable"
    assert W[JUNK]["tier"] == "unknown", "explorer-page pending entries are not evidence"
    assert W[METERED]["tier"] == "behavioral" and "metered" in W[METERED]["category"]
    assert "batch-channel-receiver" in W[METERED]["roles"]
    assert W[STRANGER]["tier"] == "unknown"
    assert W[CLONE]["tier"] == "hard-stop", W[CLONE]
    assert W[ESCROW]["tier"] == "protocol" and "x402-contract" in W[ESCROW]["roles"]
    assert W[FAC]["tier"] == "verified" and "facilitator" in W[FAC]["roles"]
    assert "0x9999999999999999999999999999999999999999" not in W, "unsupported chains skipped"
    assert W[AGENT2]["tier"] == "role-only" and W[AGENT2]["name"] == ""
    assert "channel-depositor" in W[AGENT2]["roles"]
    assert W[AGENT2]["as_buyer"]["class"] in ("multi-seller agent", "agent")
    assert W[LOOPER]["as_buyer"]["class"] == "single-seller buyer"

    P = {(p["payer"], p["payee"], p["leg_type"]): p for p in res["payments"]}
    dep = P[(AGENT2, ESCROW, "batch-deposit")]
    assert dep["account"] == L.ESCROW_ACCOUNT and dep["category"] == L.CAT_ESCROW_DEPOSIT, \
        "a channel deposit is escrow, never expense"
    assert P[(AGENT, VENDOR, "exact")]["account"] == "6410 - Data"
    assert P[(AGENT, METERED, "exact")]["account"].startswith("6480")
    assert "self-payment" in P[(AGENT, AGENT, "exact")]["flags"]
    assert "dust" in P[(AGENT, CLONE, "exact")]["flags"]
    loop = P[(LOOPER, LOOPSELLER, "exact")]
    assert "closed-loop" in loop["flags"] and not L.excluded(loop), \
        "closed-loop is review-only, never silently excluded"

    cov = res["coverage"]["overall"]
    pay_n = sum(1 for e in evs if L.leg_type(e.memo) in L.PAYMENT_LEGS)
    assert cov["payments"]["settlements"] == pay_n, "escrow legs are not payments"
    assert cov["flagged"]["settlements"] == 2           # self-payment + dust
    unk = res["coverage"]["work_queue"]
    assert unk[0]["wallet"] == STRANGER, "work queue is ordered by dollars"
    assert {w["wallet"] for w in unk} >= {STRANGER, METERED, JUNK, LOOPSELLER}

    pub = L.public_labels(res)
    pw = {w["wallet"] for w in pub["wallets"]}
    assert VENDOR in pw and FAC in pw and ESCROW in pw
    assert AGENT2 not in pw and LOOPER not in pw, "buyers are never published"

    # --- persistence round-trip into the ledger db ---
    d = tempfile.mkdtemp()
    db = os.path.join(d, "t.db")
    st = EventStore(db)
    st.add_events(evs)
    st.close()
    out = L.run_labels(write=False, db_path=db)
    assert out["coverage"]["overall"]["payments"]["settlements"] == pay_n
    conn = sqlite3.connect(db)
    L.persist(conn, res, ["base"])
    n_w = conn.execute("SELECT COUNT(*) FROM wallet_labels").fetchone()[0]
    n_p = conn.execute("SELECT COUNT(*) FROM payment_labels").fetchone()[0]
    tier = conn.execute("SELECT tier FROM wallet_labels WHERE wallet=?", (CLONE,)).fetchone()[0]
    L.persist(conn, res, ["base"])                       # idempotent re-run
    assert conn.execute("SELECT COUNT(*) FROM payment_labels").fetchone()[0] == n_p
    assert n_p == len(evs) and tier == "hard-stop" and n_w == len(W)
    stored = conn.execute("SELECT facilitator FROM events LIMIT 1").fetchone()[0]
    assert stored == "Coinbase", "facilitator name persists in the ledger"

    print("ALL LABEL TESTS PASSED")
    print("  tiers: hard-stop / verified / protocol / probable / behavioral / unknown / role-only")
    print("  escrow deposits booked to escrow; closed-loop review-only; buyers unpublished")
    print("  persistence: wallet_labels + payment_labels, idempotent")


if __name__ == "__main__":
    main()
