"""
Tests for accounting exports (exports.py) — QuickBooks and Xero CSV formats.

Verifies the critical invariants:
  - each Counterra entry expands to exactly 2 lines (debit + credit)
  - debits equal credits (balanced) for every entry
  - Xero continuation rows have blank Date + Narration (grouping rule)
  - Xero uses numeric account codes; headers match Xero's exact template
  - QuickBooks pairs share a Journal No.
"""
import csv
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from counterralib.exports import (
    write_quickbooks_csv, write_xero_csv, _account_code,
)

SAMPLE = [
    {"period": "2026-07", "debit_account": "6490 - Uncategorized Agent Spend",
     "credit_account": "1085 - Digital Assets (USDC)", "amount_usd": 6.01,
     "provider": "0x68396bd3…", "provider_wallet": "0x68396bd3",
     "category": "Uncategorized", "settlements": 5,
     "memo": "Agent spend 2026-07"},
    {"period": "2026-07", "debit_account": "6200 - Market Data",
     "credit_account": "1085 - Digital Assets (USDC)", "amount_usd": 12.50,
     "provider": "Laevitas", "provider_wallet": "0xABC",
     "category": "Market data", "settlements": 2,
     "memo": "Agent spend 2026-07"},
]


def _read(path):
    with open(path) as f:
        return list(csv.reader(f))


def test_account_code_extraction():
    assert _account_code("1085 - Digital Assets (USDC)") == "1085"
    assert _account_code("6490 - Uncategorized Agent Spend") == "6490"
    assert _account_code("NoNumberHere") == "NoNumberHere"
    print("account code extraction OK")


def test_quickbooks_two_lines_per_entry():
    fd, p = tempfile.mkstemp(suffix=".csv"); os.close(fd)
    write_quickbooks_csv(SAMPLE, p)
    rows = _read(p)
    header, data = rows[0], rows[1:]
    assert header == ["Journal No.", "Journal Date", "Account",
                      "Debit", "Credit", "Description", "Memo"]
    assert len(data) == 4, f"2 entries -> 4 lines, got {len(data)}"
    # First entry's two lines share a Journal No.
    assert data[0][0] == data[1][0], "debit/credit lines must share Journal No."
    assert data[0][0] != data[2][0], "different entries -> different Journal No."
    os.unlink(p)
    print("QuickBooks 2-line expansion + shared Journal No. OK")


def test_quickbooks_balanced():
    fd, p = tempfile.mkstemp(suffix=".csv"); os.close(fd)
    write_quickbooks_csv(SAMPLE, p)
    rows = _read(p)[1:]
    # For each pair, debit on line 1 == credit on line 2
    for i in range(0, len(rows), 2):
        debit = float(rows[i][3])
        credit = float(rows[i + 1][4])
        assert abs(debit - credit) < 0.001, f"unbalanced: {debit} vs {credit}"
    os.unlink(p)
    print("QuickBooks debits == credits OK")


def test_xero_headers_exact():
    fd, p = tempfile.mkstemp(suffix=".csv"); os.close(fd)
    write_xero_csv(SAMPLE, p)
    header = _read(p)[0]
    # Xero fails the import if headers don't match its template exactly.
    assert header == ["*Narration", "*Date", "*AccountCode",
                      "Description", "*Debit", "*Credit"], header
    os.unlink(p)
    print("Xero exact header names OK")


def test_xero_continuation_rows_blank():
    fd, p = tempfile.mkstemp(suffix=".csv"); os.close(fd)
    write_xero_csv(SAMPLE, p)
    data = _read(p)[1:]
    assert len(data) == 4
    # Line 1 of entry 1: has narration + date
    assert data[0][0] != "" and data[0][1] != ""
    # Line 2 (continuation): blank narration + date so Xero groups them
    assert data[1][0] == "" and data[1][1] == "", "continuation must be blank"
    os.unlink(p)
    print("Xero continuation-row grouping OK")


def test_xero_uses_numeric_codes():
    fd, p = tempfile.mkstemp(suffix=".csv"); os.close(fd)
    write_xero_csv(SAMPLE, p)
    data = _read(p)[1:]
    # AccountCode column should be the numeric code, not the full label
    assert data[0][2] == "6490", data[0][2]
    assert data[1][2] == "1085", data[1][2]
    os.unlink(p)
    print("Xero numeric account codes OK")


def test_xero_balanced():
    fd, p = tempfile.mkstemp(suffix=".csv"); os.close(fd)
    write_xero_csv(SAMPLE, p)
    data = _read(p)[1:]
    for i in range(0, len(data), 2):
        debit = float(data[i][4])
        credit = float(data[i + 1][5])
        assert abs(debit - credit) < 0.001
    os.unlink(p)
    print("Xero debits == credits OK")


def test_empty_entries():
    fd, p = tempfile.mkstemp(suffix=".csv"); os.close(fd)
    write_quickbooks_csv([], p)
    assert len(_read(p)) == 1  # header only
    write_xero_csv([], p)
    assert len(_read(p)) == 1
    os.unlink(p)
    print("empty entries -> header-only OK")


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
    print("\nALL EXPORT TESTS PASSED")
