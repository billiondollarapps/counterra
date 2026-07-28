"""
Tests for glbook.py — the general-ledger / trial-balance engine.

The central guarantee: a trial balance built from Counterra journal entries is
BALANCED (total debits == total credits). If that invariant ever breaks, the
sub-ledger can't be trusted, so it's the first and most important test.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from counterralib import glbook as gl


def _entry(debit, credit, amount, provider="seller.com", period="2026-07"):
    return {"debit_account": debit, "credit_account": credit,
            "amount_usd": amount, "provider": provider, "period": period}


ASSET = "1085 - Digital Assets (USDC)"


def test_trial_balance_balances():
    entries = [
        _entry("6410 - Data & Research Services", ASSET, 12.50),
        _entry("6420 - AI & Compute Services", ASSET, 3.00),
        _entry("6490 - Uncategorized Agent Spend", ASSET, 0.50),
    ]
    tb = gl.trial_balance(entries)
    assert tb["balanced"], tb
    assert tb["total_debit"] == 16.00
    assert tb["total_credit"] == 16.00
    assert tb["out_of_balance"] == 0.0
    print("trial balance balances OK")


def test_expense_accounts_carry_debit_balances():
    entries = [_entry("6410 - Data", ASSET, 10.0),
               _entry("6410 - Data", ASSET, 5.0)]
    tb = gl.trial_balance(entries)
    data = [a for a in tb["accounts"] if a["account"].startswith("6410")][0]
    assert data["debit"] == 15.0 and data["credit"] == 0.0
    assert data["balance"] == 15.0  # debit-normal
    assert data["class"] == "expense"
    print("expense debit balances OK")


def test_asset_account_carries_credit_total():
    entries = [_entry("6410 - Data", ASSET, 10.0),
               _entry("6420 - AI", ASSET, 5.0)]
    tb = gl.trial_balance(entries)
    asset = [a for a in tb["accounts"] if a["account"] == ASSET][0]
    assert asset["credit"] == 15.0 and asset["debit"] == 0.0
    assert asset["class"] == "asset"
    print("asset credit total OK")


def test_empty_entries_balances_trivially():
    tb = gl.trial_balance([])
    assert tb["balanced"] and tb["total_debit"] == 0.0
    assert tb["accounts"] == []
    print("empty trial balance OK")


def test_classify_account():
    assert gl.classify_account("6410 - x") == "expense"
    assert gl.classify_account("1085 - x") == "asset"
    assert gl.classify_account("9999 - x") == "other"
    assert gl.classify_account("") == "other"
    print("account classification OK")


def test_account_ledger_drilldown():
    entries = [
        _entry("6410 - Data", ASSET, 10.0, provider="blockrun.ai"),
        _entry("6410 - Data", ASSET, 5.0, provider="laevitas"),
        _entry("6420 - AI", ASSET, 3.0, provider="vaaya"),
    ]
    rows = gl.account_ledger(entries, "6410 - Data")
    assert len(rows) == 2  # only the two 6410 postings
    assert rows[0]["debit"] == 10.0
    assert rows[-1]["running_balance"] == 15.0  # accumulates
    print("account drill-down OK")


def test_asset_ledger_shows_all_disposals():
    entries = [_entry("6410 - Data", ASSET, 10.0),
               _entry("6420 - AI", ASSET, 5.0)]
    rows = gl.account_ledger(entries, ASSET)
    assert len(rows) == 2
    # asset account is credited each time
    assert all(r["credit"] > 0 for r in rows)
    print("asset ledger disposals OK")


def test_summary_string():
    entries = [_entry("6410 - Data", ASSET, 10.0)]
    s = gl.summarize_trial_balance(gl.trial_balance(entries))
    assert "BALANCED" in s
    assert "$10.00" in s
    print("summary string OK")


def test_many_entries_still_balance():
    # stress: lots of entries across accounts must still net to zero
    import random
    random.seed(42)
    accts = ["6410 - A", "6420 - B", "6430 - C", "6490 - Uncat"]
    entries = [_entry(random.choice(accts), ASSET, round(random.uniform(0.001, 50), 2))
               for _ in range(500)]
    tb = gl.trial_balance(entries)
    assert tb["balanced"], f"out of balance by {tb['out_of_balance']}"
    print("500-entry balance OK")


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
    print("\nALL GL/TRIAL-BALANCE TESTS PASSED")
