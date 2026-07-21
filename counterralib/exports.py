"""
Accounting exports for Counterra — turn journal entries into files that
QuickBooks Online and Xero import directly.

Counterra's internal journal entry packs a debit account and a credit account
onto ONE row (compact, readable). Both QuickBooks and Xero, however, expect
each journal entry as TWO lines — one debit line, one credit line — sharing a
reference so the importer groups them. These exporters expand each Counterra
entry into that two-line shape, in each platform's exact required columns.

QuickBooks Online (mapping-based importer, tolerant of column names):
    Journal No., Journal Date, Account, Debit, Credit, Description, Memo
    - each entry -> 2 rows sharing Journal No.; debit row then credit row.

Xero (strict: headers must match its template EXACTLY or the import fails;
a row with blank Date+Narration continues the journal above it):
    *Narration, *Date, *AccountCode, Description, *Debit, *Credit
    - first line of an entry carries Narration + Date; the paired line leaves
      both blank so Xero groups them into one manual journal.
    - NOTE: Xero keys accounts by AccountCode, so we emit the numeric code
      parsed from Counterra's "1085 - Digital Assets (USDC)" style strings.

Both are double-entry balanced by construction: debit total == credit total
for every entry (single settlement group), so imports never fail on balance.
"""

from __future__ import annotations

import csv
import re
from datetime import datetime


def _account_code(account_str: str) -> str:
    """
    Extract the leading numeric code from a Counterra account label.
    "1085 - Digital Assets (USDC)" -> "1085"; falls back to the whole string.
    """
    m = re.match(r"\s*(\d+)", account_str or "")
    return m.group(1) if m else (account_str or "").strip()


def _period_to_date(period: str) -> str:
    """
    Counterra periods are 'YYYY-MM'. Accounting imports want a real date;
    use the last day of the month is overkill — first day is conventional and
    unambiguous. Returns MM/DD/YYYY (QuickBooks) or DD/MM/YYYY handled by caller.
    """
    try:
        dt = datetime.strptime(period + "-01", "%Y-%m-%d")
        return dt
    except ValueError:
        return datetime.today()


def write_quickbooks_csv(entries, path, date_format="%m/%d/%Y"):
    """
    Write QuickBooks Online journal-import CSV.

    Each Counterra entry becomes two rows (debit line, credit line) sharing a
    Journal No. Debit/Credit are separate columns; the unused one is blank.
    """
    headers = ["Journal No.", "Journal Date", "Account",
               "Debit", "Credit", "Description", "Memo"]
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(headers)
        for i, e in enumerate(entries, 1):
            jno = f"CTR-{e['period'].replace('-', '')}-{i:04d}"
            jdate = _period_to_date(e["period"]).strftime(date_format)
            amt = f"{e['amount_usd']:.2f}"
            desc = f"{e['provider']} - {e['category']} ({e['settlements']} settlements)"
            memo = e.get("memo", "")
            # Debit line: the expense/category account
            w.writerow([jno, jdate, e["debit_account"], amt, "", desc, memo])
            # Credit line: the digital-asset (USDC) account
            w.writerow([jno, jdate, e["credit_account"], "", amt, desc, memo])


def write_xero_csv(entries, path, date_format="%d/%m/%Y"):
    """
    Write Xero manual-journal import CSV.

    Xero groups lines into one journal when the continuation row has blank
    Date and Narration. Header names are Xero's exact template names — do not
    rename them. AccountCode is the numeric code Xero keys on.
    """
    headers = ["*Narration", "*Date", "*AccountCode",
               "Description", "*Debit", "*Credit"]
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(headers)
        for i, e in enumerate(entries, 1):
            date = _period_to_date(e["period"]).strftime(date_format)
            narration = (f"Counterra agent spend {e['period']} - "
                         f"{e['provider']} ({e['settlements']} settlements)")
            amt = f"{e['amount_usd']:.2f}"
            desc = f"{e['provider']} - {e['category']}"
            debit_code = _account_code(e["debit_account"])
            credit_code = _account_code(e["credit_account"])
            # First line of the journal: Narration + Date present. Debit side.
            w.writerow([narration, date, debit_code, desc, amt, "0.00"])
            # Continuation line: blank Narration + Date -> same journal. Credit side.
            w.writerow(["", "", credit_code, desc, "0.00", amt])


def export_all(entries, out_dir, base_name="journal"):
    """Convenience: write both formats, return the two paths."""
    import os
    qb = os.path.join(out_dir, f"{base_name}_quickbooks.csv")
    xr = os.path.join(out_dir, f"{base_name}_xero.csv")
    write_quickbooks_csv(entries, qb)
    write_xero_csv(entries, xr)
    return qb, xr
