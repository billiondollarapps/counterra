"""
Offline tests for the network explorer builder (counterralib/explorer.py).
Run:  python3 tests/test_explorer.py
"""
import csv
import datetime
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from counterralib import explorer as E
from counterralib import labels as L
from counterralib.ingest import PaymentEvent

NOW = datetime.datetime(2026, 10, 26, 12, tzinfo=datetime.timezone.utc)
VENDOR = "0xa11ce00000000000000000000000000000000001"
SOL = "8mTdwZCZDPeDhso3iAk2znuuzWWXrq7Cni67WJ6Tc167"
A1 = "0x1111000000000000000000000000000000000aa1"
A2 = "0x2222000000000000000000000000000000000aa2"


def ev(i, payer, payee, amt, days_ago, chain="base", memo="x402:exact"):
    return PaymentEvent(tx_hash="tx%03d" % i, ts=NOW - datetime.timedelta(days=days_ago, minutes=i),
                        chain=chain, payer_wallet=payer, payee_wallet=payee, amount_usdc=amt,
                        protocol="x402", memo=memo, facilitator="Coinbase")


def main():
    evs = [ev(1, A1, VENDOR, 1.0, 40), ev(2, A1, VENDOR, 2.0, 10), ev(3, A2, VENDOR, 3.0, 2),
           ev(4, A1, A1, 9.0, 1),                                        # self-payment: excluded
           ev(5, "BuyerSol111", SOL, 0.5, 3, chain="solana"),
           ev(6, A2, "0x4020074e9df2ce1dee5a9c1b5c3f541d02a10003", 50.0, 1,
              memo="x402:batch-deposit")]                                # escrow: not revenue
    reg = {"providers": [{"wallet": VENDOR, "chain": "base", "label": "Vendor <b>Co</b>",
                          "category": "Market data", "evidence": "catalog"}]}
    res = L.build(evs, registry=reg)
    out = E.build(res["payments"], res["wallets"], res["coverage"], now=NOW)

    n = out["network"]
    assert n["totals"]["settlements"] == 4 and n["totals"]["usd"] == 6.5, n["totals"]
    assert n["chains"]["base"]["sellers"] == 1 and n["chains"]["solana"]["usd"] == 0.5
    assert sum(d["settlements"] for d in n["daily"]) == 4

    s = {x["wallet"]: x for x in out["sellers"]}
    v = s[VENDOR]
    assert (v["usd"], v["usd_30d"], v["usd_7d"], v["buyers"]) == (6.0, 5.0, 3.0, 2), v
    assert v["tier"] == "verified" and v["has_detail"] and v["has_revenue_csv"]
    assert out["sellers"][0]["wallet"] == VENDOR, "sorted by revenue"

    det = out["details"][E.slug(VENDOR)]
    assert det["top_buyers"][0]["wallet"] == A1 and det["buyer_concentration_pct"] == 50.0
    assert det["recent"][0]["tx"] == "tx003", "recent is newest first"
    assert E.slug("../../x?y") == "xy", "slugs cannot escape the data folder"

    d = tempfile.mkdtemp()
    counts = E.write(out, data_dir=d)
    assert counts["details"] == 2
    assert json.load(open(os.path.join(d, "sellers.json")))["sellers"][0]["name"] == "Vendor <b>Co</b>"
    rows = list(csv.reader(open(os.path.join(d, "revenue", E.slug(VENDOR) + ".csv"))))
    journal = [r for r in rows if r and r[0] == "journal"]
    pays = [r for r in rows if r and r[0] == "payment"]
    assert [r[1] for r in journal] == ["2026-09", "2026-10"], journal
    assert journal[1][2] == E.ASSET_ACCOUNT and journal[1][3] == E.REVENUE_ACCOUNT
    assert abs(sum(float(r[4]) for r in journal) - 6.0) < 1e-9
    assert len(pays) == 3 and abs(sum(float(r[4]) for r in pays) - 6.0) < 1e-9, \
        "journal totals reconcile to the payment lines"

    # a seller that drops out of the dataset loses its page on the next write
    open(os.path.join(d, "sellers", "stale.json"), "w").write("{}")
    E.write(out, data_dir=d)
    assert not os.path.exists(os.path.join(d, "sellers", "stale.json"))

    print("ALL EXPLORER TESTS PASSED")
    print("  network totals exclude self-payments and escrow; 30d/7d windows")
    print("  revenue ledger: monthly journal reconciles to payment lines")
    print("  stale seller pages removed on rebuild; slugs are path-safe")


if __name__ == "__main__":
    main()
