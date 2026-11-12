"""
Offline tests for monthly statement packs (counterralib/statements.py).
Run:  python3 tests/test_statements.py
"""
import csv
import datetime
import json
import os
import sys
import tempfile
import zipfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from counterralib import labels as L
from counterralib import statements as S
from counterralib.ingest import PaymentEvent

T0 = datetime.datetime(2026, 9, 3, tzinfo=datetime.timezone.utc)
SELLER = "0xa11ce00000000000000000000000000000000001"
OTHER = "0xb0b0000000000000000000000000000000000002"
CLONE = "0xa11ce0000000000000000000000000000ff00001"
B1 = "0x1111000000000000000000000000000000000aa1"
B2 = "0x2222000000000000000000000000000000000aa2"
B3 = "0x3333000000000000000000000000000000000aa3"
ESCROW = "0x4020074e9df2ce1dee5a9c1b5c3f541d02a10003"


def ev(i, payer, payee, amt, day=0, memo="x402:exact"):
    return PaymentEvent(tx_hash="0x%064x" % i, ts=T0 + datetime.timedelta(days=day, minutes=i),
                        chain="base", payer_wallet=payer, payee_wallet=payee, amount_usdc=amt,
                        protocol="x402", memo=memo, facilitator="Coinbase")


def fixture():
    evs, i = [], 0
    for _ in range(3):            # three buyers each pay 1/3 cent -> rounding stress
        for b in (B1, B2, B3):
            i += 1; evs.append(ev(i, b, SELLER, 0.003333))
    for _ in range(4):            # B1 also buys more, at two price points
        i += 1; evs.append(ev(i, B1, SELLER, 0.25, day=2))
    i += 1; evs.append(ev(i, B1, SELLER, 45.0, day=5))             # large payment
    for _ in range(3):
        i += 1; evs.append(ev(i, B1, OTHER, 0.1, day=6))            # B1 buys elsewhere
    i += 1; evs.append(ev(i, B1, CLONE, 0.000001, day=7))           # poisoning dust
    i += 1; evs.append(ev(i, B1, ESCROW, 5.0, day=8, memo="x402:batch-deposit"))
    i += 1; evs.append(ev(i, B2, SELLER, 7.0, day=40))              # next month: excluded
    return evs


REG = {"providers": [{"wallet": SELLER, "chain": "base", "label": "Vendor Co",
                      "category": "Market data", "evidence": "catalog"}]}


def main():
    # largest-remainder rounding: parts always sum to the rounded total
    for xs in ([0.003333] * 9, [0.005, 0.005, 0.005], [1.004, 2.004, 3.004], [0.0]):
        parts = S.allocate_cents(xs)
        assert abs(sum(parts) - S.cents(sum(xs))) < 1e-9, (xs, parts)
    assert S.cents(0.125) == 0.13 and S.cents(2.675) == 2.68, "half-up, not banker's"

    res = L.build(fixture(), registry=REG,
                  accounting={"expense_accounts": {"Market data": "6410 - Data & Research Services"}})

    from counterralib.rates import RateBook
    # USDC traded at 0.998 on 2026-09-08 (the $45 payment day); 09-05 has no rate -> par fallback
    rb = RateBook({"tokens": {"USDC": {"2026-09-03": {"rate": 1.0, "points": 24},
                                       "2026-09-08": {"rate": 0.998, "points": 24}}}})

    # ---- seller pack, default: identified customers + one summary invoice ----
    cust = {B2: {"name": "Acme Agents Ltd", "tax_id": "DE999"}}
    pk = S.build_pack(SELLER, "2026-09", res, rates=rb, customers=cust)
    assert pk["role"] == "seller" and pk["name"] == "Vendor Co"
    usdc = 0.003333 * 9 + 0.25 * 4 + 45.0
    usd = 0.003333 * 9 + 0.25 * 4 + 45.0 * 0.998
    assert abs(pk["tie_out"]["payments_exact"] - round(usdc, 6)) < 1e-9
    assert abs(pk["tie_out"]["usd_exact"] - usd) < 1e-6
    assert pk["summary"]["total"] == S.cents(usd) == 45.94, pk["summary"]
    assert pk["summary"]["counterparties"] == 3 and len(pk["invoices"]) == 2, \
        "one invoice for the named customer + one summary for anonymous buyers"
    by = {i["number"]: i for i in pk["invoices"]}
    acme = by["CTR-202609-000001-000AA2"]
    assert acme["buyer"]["name"] == "Acme Agents Ltd" and acme["buyer"]["tax_id"] == "DE999"
    anon = by["CTR-202609-000001-ANON"]
    assert anon["summary"] and anon["buyer"]["wallets"] == 2 and len(anon["schedule"]) == 2
    assert [l["quantity"] for l in anon["lines"]] == [1, 4, 6], "lines grouped by unit price"
    assert {c["name"] for c in pk["counterparties"]} == {"Acme Agents Ltd", ""}, \
        "only names the seller supplied; buyers are never named by Counterra"
    assert len(pk["journal"]) == 2 and all(j["credit_account"] == S.REVENUE_ACCOUNT for j in pk["journal"])
    types = [x["type"] for x in pk["exceptions"]]
    assert "large payment" in types and "customer concentration" in types, types
    assert "exchange rate unavailable" in types, "par fallback days must be reported"

    # per-buyer mode reproduces one invoice per wallet
    pb = S.build_pack(SELLER, "2026-09", res, rates=rb, invoice_mode="per-buyer")
    assert len(pb["invoices"]) == 3 and pb["invoices"][0]["number"] == "CTR-202609-000001-000AA1"
    assert pb["summary"]["total"] == pk["summary"]["total"]

    # ---- buyer pack: unidentified sellers go to suspense ----
    bk = S.build_pack(B1, "2026-09", res, rates=rb)
    assert bk["role"] == "buyer"
    cps = {c["wallet"]: c for c in bk["counterparties"]}
    assert cps[SELLER]["name"] == "Vendor Co" and cps[SELLER]["account"].startswith("6410")
    assert cps[OTHER]["group"] == "suspense" and cps[OTHER]["account"] == S.SUSPENSE_ACCOUNT
    assert cps[CLONE]["account"].startswith("7910"), "poisoning is held, not suspense"
    assert ESCROW not in cps, "escrow deposit is not a seller"
    assert bk["summary"]["escrow_deposits"] == 5.0 and bk["summary"]["suspense"] == 1
    bt = [x["type"] for x in bk["exceptions"]]
    assert bt[0] == "suspected address poisoning", "hard-stops sort first"
    assert "unidentified seller (in suspense)" in bt and "batch-channel deposit (escrow)" in bt
    assert any(j["debit_account"] == L.ESCROW_ACCOUNT for j in bk["journal"])
    assert any(j["debit_account"] == S.SUSPENSE_ACCOUNT for j in bk["journal"])
    assert bk["invoices"][0]["kind"] == "reconstructed"

    # ---- written pack: files exist, re-read checks pass, zip complete ----
    d = tempfile.mkdtemp()
    issuer = {"name": "Vendor Co Ltd", "address": "1 Main St", "tax_id": "GB123"}
    pk2 = S.build_pack(SELLER, "2026-09", res, issuer=issuer, rates=rb, customers=cust)
    assert by and pk2["invoices"][0]["tax_ready"] != pk2["invoices"][-1]["tax_ready"], \
        "named customer invoice is tax-ready; anonymous summary is not"
    S.write_pack(pk2, d)
    for f in ("index.html", "pack.json", "payments.csv", "exceptions.csv", "customer_ledger.csv",
              "rates.csv", "journal_quickbooks.csv", "journal_xero.csv", "pack.zip"):
        assert os.path.exists(os.path.join(d, f)), f
    assert len(os.listdir(os.path.join(d, "invoices"))) == 2
    t = json.load(open(os.path.join(d, "pack.json")))["tie_out"]
    assert t["all_pass"] and len(t["checks"]) == 8, t
    with open(os.path.join(d, "payments.csv")) as f:
        rows = list(csv.DictReader(f))
    assert len(rows) == 14 and all(r["invoice"] for r in rows), "every payment maps to an invoice"
    big = [r for r in rows if r["amount_usdc"] == "45.000000"][0]
    assert big["rate_to_usd"] == "0.99800000" and big["amount_usd"] == "44.910000"
    with open(os.path.join(d, "customer_ledger.csv")) as f:
        cl = list(csv.DictReader(f))
    assert len(cl) == 3 and {r["customer"] for r in cl} == {"Acme Agents Ltd", S.ANON_CUSTOMER}
    names = zipfile.ZipFile(os.path.join(d, "pack.zip")).namelist()
    assert "index.html" in names and any(n.startswith("invoices/") for n in names)
    page = open(os.path.join(d, "invoices", "CTR-202609-000001-000AA2.html")).read()
    assert "Tax ID: GB123" in page and "Tax ID: DE999" in page and "Payment-backed record" not in page

    bd = tempfile.mkdtemp()
    S.write_pack(bk, bd)
    with open(os.path.join(bd, "suspense_ledger.csv")) as f:
        sl = list(csv.DictReader(f))
    assert len(sl) == 1 and sl[0]["seller_wallet"] == OTHER and sl[0]["status"].startswith("open")

    # a corrupted export is caught by the read-back check
    S.write_quickbooks_csv(pk2["journal"][:-1], os.path.join(d, "journal_quickbooks.csv"))
    pk3 = json.loads(json.dumps(pk2))
    pk3["tie_out"]["checks"] = [c for c in pk3["tie_out"]["checks"]
                                if "file" not in c["check"]]
    S.verify_exports(pk3, os.path.join(d, "journal_quickbooks.csv"),
                     os.path.join(d, "journal_xero.csv"))
    assert not pk3["tie_out"]["all_pass"], "tie-out must fail when an export is short"

    try:
        S.build_pack("0x9999999999999999999999999999999999999999", "2026-09", res)
        raise AssertionError("expected no-activity error")
    except ValueError:
        pass

    print("ALL STATEMENT TESTS PASSED")
    print("  cents allocated so parts sum to the rounded total; half-up rounding")
    print("  daily-average FX translation (depeg day), par fallback flagged")
    print("  seller: named customers + one anonymous summary invoice + sub-ledger")
    print("  buyer: unidentified sellers to 1990 suspense; poisoning held in 7910")
    print("  written pack re-reads its own exports; a short export fails the tie-out")


if __name__ == "__main__":
    main()
