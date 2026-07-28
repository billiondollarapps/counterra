"""
General ledger & trial balance — turn journal entries into a real sub-ledger.

A pile of journal entries is not a set of books. A sub-ledger is: entries posted
into running per-account balances, provably balanced (total debits == total
credits), that roll up into a trial balance an accountant recognises and an
auditor can trace. This module is what makes Counterra the agent-spend
SUB-LEDGER rather than a per-wallet snapshot.

Scope, stated honestly: Counterra is the sub-ledger for agent payments — the one
ledger a company's general ledger (QuickBooks/Xero) can't produce itself,
because it requires decoding on-chain micropayments. It is NOT the company's
whole books. It handles two sides:

    Dr  <expense account by seller category>     (what the spend was for)
    Cr  Digital Assets (USDC)                    (the asset disposed to pay)

So within this sub-ledger, every expense account carries a debit balance and the
USDC asset account carries the matching total credit balance. Debits == credits
by construction (double-entry), and the trial balance proves it — if it ever
doesn't balance, something is wrong upstream and the books can't be trusted.

The buyer-side context (which wallets are THIS company's agents, and their own
category names) is supplied by the company at onboarding — it cannot be scraped.
The public registry supplies the seller side automatically. This engine operates
on whatever journal entries it is given, so it serves a single configured
company's declared wallets.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Dict, List, Optional


# Account classes for a trial balance. Expense accounts are debit-normal;
# the digital-asset account is an asset (debit-normal) but within THIS
# sub-ledger it only ever receives credits (disposals), so it shows as a
# credit total — the contra to all the expense debits.
def classify_account(account):
    """
    Rough account class from its code prefix, for trial-balance grouping.
    6xxx = expense, 1xxx = asset, else unknown. Codes look like "6410 - ...".
    """
    code = (account or "").strip()[:1]
    if code == "6":
        return "expense"
    if code == "1":
        return "asset"
    return "other"


def post_entries(journal_entries):
    """
    Post journal entries into running per-account debit/credit totals.

    Each entry is one settlement group: a debit to an expense account and a
    credit to the asset account, for the same amount. Returns a dict:
      {account: {"debit": float, "credit": float, "class": str, "n": int}}
    """
    ledger = defaultdict(lambda: {"debit": 0.0, "credit": 0.0, "class": None, "n": 0})
    for e in journal_entries:
        amt = float(e["amount_usd"])
        d, c = e["debit_account"], e["credit_account"]
        ledger[d]["debit"] += amt
        ledger[d]["class"] = classify_account(d)
        ledger[d]["n"] += 1
        ledger[c]["credit"] += amt
        ledger[c]["class"] = classify_account(c)
        ledger[c]["n"] += 1
    # round for presentation
    for acc in ledger.values():
        acc["debit"] = round(acc["debit"], 2)
        acc["credit"] = round(acc["credit"], 2)
    return dict(ledger)


def trial_balance(journal_entries):
    """
    Produce a trial balance from journal entries.

    Returns:
      {
        "accounts": [ {account, class, debit, credit, balance, n}, ... ],
        "total_debit": float,
        "total_credit": float,
        "balanced": bool,          # the integrity check: debits == credits
        "out_of_balance": float,   # difference if not balanced (should be 0.00)
      }

    `balanced` is the whole point: a sub-ledger you can trust is one where the
    trial balance proves every debit has its matching credit. If this is False,
    the books are wrong and must not be relied on.
    """
    ledger = post_entries(journal_entries)
    accounts = []
    total_debit = total_credit = 0.0
    for name, a in sorted(ledger.items(), key=lambda kv: kv[0]):
        # net balance on the account's normal side
        bal = round(a["debit"] - a["credit"], 2)
        accounts.append({
            "account": name,
            "class": a["class"],
            "debit": a["debit"],
            "credit": a["credit"],
            "balance": bal,
            "n": a["n"],
        })
        total_debit += a["debit"]
        total_credit += a["credit"]
    total_debit = round(total_debit, 2)
    total_credit = round(total_credit, 2)
    diff = round(total_debit - total_credit, 2)
    return {
        "accounts": accounts,
        "total_debit": total_debit,
        "total_credit": total_credit,
        "balanced": abs(diff) < 0.01,
        "out_of_balance": diff,
    }


def account_ledger(journal_entries, account):
    """
    The detail ledger for one account — every entry that touched it.

    This is the drill-down an accountant does from a trial-balance line:
    "show me every posting that makes up this $X balance." Returns a list of
    {period, counterparty, debit, credit} rows plus a running balance.
    """
    rows = []
    running = 0.0
    acc_class = classify_account(account)
    for e in journal_entries:
        d, c = e["debit_account"], e["credit_account"]
        amt = float(e["amount_usd"])
        if account not in (d, c):
            continue
        debit = amt if d == account else 0.0
        credit = amt if c == account else 0.0
        # running balance on the normal side
        if acc_class == "asset":
            running += debit - credit
        else:
            running += debit - credit
        rows.append({
            "period": e.get("period", ""),
            "counterparty": e.get("provider", ""),
            "debit": round(debit, 2),
            "credit": round(credit, 2),
            "running_balance": round(running, 2),
        })
    return rows


def summarize_trial_balance(tb):
    """One-line human summary of a trial balance."""
    status = "BALANCED" if tb["balanced"] else f"OUT OF BALANCE by ${tb['out_of_balance']:.2f}"
    n_exp = len([a for a in tb["accounts"] if a["class"] == "expense"])
    return (f"Trial balance: {len(tb['accounts'])} accounts "
            f"({n_exp} expense), total Dr ${tb['total_debit']:,.2f} / "
            f"Cr ${tb['total_credit']:,.2f} — {status}")
