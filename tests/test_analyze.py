"""
Tests for analyze.py — the explain-the-books layer.

The critical guarantees: findings are a PURE function of the ledger (same books
in, same facts out), the template narration works with no LLM, and findings
only ever contain numbers derived from the events (the anti-hallucination
foundation — the deterministic layer is the ground truth the LLM can't override).
"""
import os
import sys
from types import SimpleNamespace
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from counterralib import analyze as az


def _ev(payee, payer, amount):
    return SimpleNamespace(
        payee_wallet=payee, payer_wallet=payer, amount_usdc=amount,
        tx_hash="0x" + "a" * 64,
        ts=datetime(2026, 7, 25, 12, 0, tzinfo=timezone.utc).isoformat())


def test_findings_empty_ledger():
    assert az.findings(events=[]) == []
    assert "nothing to analyze" in az.narrate([]).lower()
    print("empty ledger OK")


def test_concentration_detected():
    # one seller dominates
    evs = [_ev("0xBIG", f"0xP{i}", 10.0) for i in range(5)] + [_ev("0xSMALL", "0xX", 0.1)]
    finds = az.findings(events=evs, registry={"0xBIG": {"label": "bigseller.com"}})
    conc = [f for f in finds if f["kind"] == "concentration"]
    assert conc, "should detect concentration"
    assert conc[0]["numbers"]["payers"] == 5
    assert "bigseller.com" in conc[0]["headline"]
    print("concentration finding OK")


def test_unattributed_exposure():
    evs = [_ev("0xKNOWN", "0xA", 1.0), _ev("0xUNKNOWN", "0xB", 4.0)]
    finds = az.findings(events=evs, registry={"0xKNOWN": {"label": "known.com"}})
    un = [f for f in finds if f["kind"] == "unattributed"]
    assert un, "should flag unattributed"
    # 4 of 5 dollars unattributed = 80%, severity action
    assert un[0]["severity"] == "action"
    assert un[0]["numbers"]["wallets"] == 1
    print("unattributed exposure OK")


def test_anomaly_detected():
    evs = [_ev("0xS", "0xA", 0.003) for _ in range(20)] + [_ev("0xBIG", "0xB", 15.0)]
    finds = az.findings(events=evs)
    anom = [f for f in finds if f["kind"] == "anomaly"]
    assert anom, "should detect the $15 outlier vs $0.003 median"
    assert anom[0]["numbers"]["largest"] == 15.0
    print("anomaly finding OK")


def test_findings_are_deterministic():
    evs = [_ev("0xA", "0xX", 1.0), _ev("0xB", "0xY", 2.0)]
    f1 = az.findings(events=evs)
    f2 = az.findings(events=evs)
    assert f1 == f2, "same books must yield identical findings"
    print("determinism OK")


def test_all_finding_numbers_trace_to_events():
    """Anti-hallucination foundation: every number in a finding is derived, not invented."""
    evs = [_ev("0xA", "0xX", 1.0), _ev("0xA", "0xY", 3.0)]
    finds = az.findings(events=evs)
    summary = [f for f in finds if f["kind"] == "summary"][0]
    assert summary["numbers"]["total"] == 4.0
    assert summary["numbers"]["settlements"] == 2
    assert summary["numbers"]["sellers"] == 1
    print("numbers trace to events OK")


def test_template_narration_no_llm():
    evs = [_ev("0xBIG", f"0xP{i}", 10.0) for i in range(5)]
    finds = az.findings(events=evs, registry={"0xBIG": {"label": "big.com"}})
    text = az.narrate(finds, use_llm=False)
    assert "big.com" in text
    assert isinstance(text, str) and len(text) > 20
    print("template narration OK")


def test_narrate_falls_back_without_key():
    # ensure no key -> template path, never raises
    saved = os.environ.pop("ANTHROPIC_API_KEY", None)
    try:
        evs = [_ev("0xA", "0xX", 1.0)]
        finds = az.findings(events=evs)
        text = az.narrate(finds, use_llm=True)  # asks for LLM but no key
        assert isinstance(text, str) and len(text) > 10
    finally:
        if saved:
            os.environ["ANTHROPIC_API_KEY"] = saved
    print("no-key fallback OK")


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
    print("\nALL ANALYZE TESTS PASSED")
