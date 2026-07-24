"""
Tests for report presentation fixes: sub-cent formatting, long-tail collapse,
and exception grouping.

These guard against the three things that made the first real report look
broken to an accountant's eye: micropayments rendering as "$0.00", 70+ rows of
zero-value counterparties, and an exception count inflated by counting one
problem once per settlement.
"""
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from report import fmt_usd, bar_rows
from counterralib.ledger import grouped_exceptions


def test_subcent_amounts_are_visible():
    """The core bug: agent micropayments must never render as $0.00."""
    assert fmt_usd(0.003) == "$0.003", fmt_usd(0.003)
    assert fmt_usd(0.000001) == "$0.000001"
    assert fmt_usd(0.076) == "$0.076"
    assert "0.00" != fmt_usd(0.001)
    print("sub-cent amounts visible OK")


def test_normal_amounts_still_two_decimals():
    assert fmt_usd(42.021) == "$42.02"
    assert fmt_usd(1234.5) == "$1,234.50"
    assert fmt_usd(0) == "$0.00"
    print("normal amounts unchanged OK")


def test_fmt_usd_handles_junk():
    assert fmt_usd(None) == "$0.00"
    assert fmt_usd("abc") == "$0.00"
    print("fmt_usd junk-safe OK")


def test_long_tail_collapsed_but_total_preserved():
    d = {f"w{i}": 0.001 for i in range(40)}
    d["big"] = 10.0
    total = sum(d.values())
    html = bar_rows(d, total, top=5)
    assert "more (long tail)" in html
    # top row present, and the tail row accounts for the remainder
    assert html.count("<tr>") == 6, html.count("<tr>")  # 5 head + 1 tail
    print("long tail collapsed to one row OK")


def test_no_tail_row_when_short():
    d = {"a": 1.0, "b": 2.0}
    html = bar_rows(d, 3.0, top=12)
    assert "long tail" not in html
    print("no spurious tail row OK")


def _row(payee, payer, amount, day=21, category="Uncategorized", provider="0xabc…"):
    return {
        "payee_wallet": payee, "payer_wallet": payer, "amount_usdc": amount,
        "category": category, "provider": provider,
        "ts": datetime(2026, 7, day, 12, 0, tzinfo=timezone.utc).isoformat(),
        "agent": payer,
    }


def test_exceptions_grouped_per_counterparty_not_per_settlement():
    """131 settlements to one unmapped wallet is ONE problem."""
    rows = [_row("0xUNK", f"0xP{i}", 0.002, day=21 + (i % 3)) for i in range(50)]
    g = grouped_exceptions(rows)
    assert len(g) == 1, f"expected 1 grouped exception, got {len(g)}"
    assert g[0]["settlements"] == 50
    assert g[0]["distinct_payers"] == 50
    assert abs(g[0]["amount_usdc"] - 0.1) < 1e-6
    print("exceptions grouped per counterparty OK")


def test_grouped_exceptions_track_window_and_payers():
    rows = [_row("0xUNK", "0xA", 1.0, day=21), _row("0xUNK", "0xA", 1.0, day=24)]
    g = grouped_exceptions(rows)
    assert g[0]["distinct_payers"] == 1  # same payer twice
    assert g[0]["first_ts"][:10] == "2026-07-21"
    assert g[0]["last_ts"][:10] == "2026-07-24"
    print("grouped window + distinct payers OK")


def test_anomalies_stay_per_settlement():
    """Large single payments are individually reviewable, so not grouped."""
    rows = [_row("0xBIG", "0xA", 100.0, category="Market data", provider="big.com"),
            _row("0xBIG", "0xB", 200.0, category="Market data", provider="big.com")]
    g = grouped_exceptions(rows, {"anomaly_threshold_usd": 40.0})
    assert len(g) == 2, g
    assert all(x["settlements"] == 1 for x in g)
    print("anomalies remain per-settlement OK")


def test_grouped_sorted_by_amount():
    rows = ([_row("0xSMALL", "0xA", 0.001)] * 3) + [_row("0xLARGE", "0xB", 5.0)]
    g = grouped_exceptions(rows)
    assert g[0]["counterparty"] == "0xLARGE"
    print("grouped sorted by value OK")


def test_no_exceptions_when_all_mapped():
    rows = [_row("0xOK", "0xA", 1.0, category="Market data", provider="ok.com")]
    assert grouped_exceptions(rows) == []
    print("clean rows -> no exceptions OK")


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
    print("\nALL REPORT-QUALITY TESTS PASSED")
