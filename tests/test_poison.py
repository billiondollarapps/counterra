"""
Tests for poison.py - address-poisoning detection.

Anchored on the design partner's real finding: a settlement Counterra booked
as supplier expense was a theft, paid to a vanity clone of the partner's own
spend wallet.

The properties that matter most, in order of damage-if-wrong:
  1. Poisoning OUTRANKS a registry identification - a clone of a real vendor
     must not post as that vendor's expense.
  2. Poisoning is a HARD STOP in the exception queue, never silent.
  3. Legitimate, established counterparties are never flagged (a false
     positive here accuses a real vendor of fraud).
  4. Homoglyph/counterfeit stablecoins are recognised as not-real-value.
"""
import datetime
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from counterralib import poison as po
from counterralib.ingest import PaymentEvent
from counterralib.ledger import enrich, exceptions, grouped_exceptions

T0 = datetime.datetime(2026, 6, 26, 12, 0, 0)

# An established vendor the agent really pays, and a vanity clone of it:
# same first 4 and last 4 hex chars, different middle.
REAL = "0x4c1a4fce51fea51f128a01cce8becb106d391155"
CLONE = "0x4c1affffffffffffffffffffffffffffffd91155"
SPEND = "0xc4c4138cf1cb7db0a48476b2c808cb3ce0dd1f80"
SPEND_CLONE = "0xc4c40000000000000000000000000000000d1f80"
UNRELATED = "0x9999888877776666555544443333222211110000"


def _ev(payee, amt, secs=0, payer=SPEND):
    return PaymentEvent(
        tx_hash="0x" + os.urandom(6).hex(),
        ts=T0 + datetime.timedelta(seconds=secs), chain="base",
        payer_wallet=payer, payee_wallet=payee, amount_usdc=amt,
        protocol="x402", memo="")


def test_lookalike_scoring():
    s = po.lookalike_score(CLONE, REAL)
    assert s["confidence"] == "high", s
    assert s["prefix"] >= 4 and s["suffix"] >= 4
    # identical address is the same wallet, not an impersonation
    assert po.lookalike_score(REAL, REAL)["confidence"] is None
    # unrelated address scores nothing
    assert po.lookalike_score(UNRELATED, REAL)["confidence"] is None
    print("lookalike scoring OK")


def test_cross_chain_not_confusable():
    assert po.lookalike_score("SoLanaAddr1111111111111111111", REAL)["confidence"] is None
    print("cross-chain addresses not compared OK")


def test_detects_partner_case():
    # 22 real settlements to REAL; one $1.88 to its clone.
    evs = [_ev(REAL, 0.001, i) for i in range(22)] + [_ev(CLONE, 1.88, 999)]
    f = po.detect_poisoning(evs)
    assert CLONE in f, f
    assert f[CLONE]["confidence"] == "high"
    assert f[CLONE]["impersonates"] == REAL
    assert f[CLONE]["amount_usd"] == 1.88
    assert "address-poisoning clone" in f[CLONE]["rationale"]
    assert REAL not in f          # the real vendor must never be flagged
    print("partner's $1.88 poisoning case detected OK")


def test_detects_clone_of_own_spend_wallet():
    # The partner's actual case: the clone mimicked their OWN wallet.
    evs = [_ev(REAL, 0.01, i) for i in range(5)] + [_ev(SPEND_CLONE, 1.88, 99)]
    f = po.detect_poisoning(evs)
    assert SPEND_CLONE in f, f
    assert f[SPEND_CLONE]["impersonates"] == SPEND.lower()
    print("clone of own spend wallet detected OK")


def test_established_vendor_never_flagged():
    evs = [_ev(REAL, 0.5, i) for i in range(50)] + [_ev(UNRELATED, 1.0, 900)]
    f = po.detect_poisoning(evs)
    assert REAL not in f and UNRELATED not in f, f
    print("established + unrelated vendors not flagged OK")


def test_registry_wallet_counts_as_established():
    # A one-off payment to a clone of a registry vendor we've never paid.
    evs = [_ev(CLONE, 2.0, 0)]
    f = po.detect_poisoning(evs, registry_wallets=[REAL])
    assert CLONE in f and f[CLONE]["impersonates"] == REAL
    print("registry wallet treated as impersonation target OK")


def test_poisoning_outranks_registry_identification():
    # Even if someone wrongly added the CLONE to the registry as a vendor,
    # the poisoning finding must win - this is the wrong-books scenario.
    evs = [_ev(REAL, 0.01, i) for i in range(5)] + [_ev(CLONE, 1.88, 99)]
    f = po.detect_poisoning(evs)
    rows = enrich(evs, {}, {CLONE: {"label": "LooksLegit",
                                    "category": "AI inference"}},
                  poison_map=f)
    bad = [r for r in rows if r["payee_wallet"].lower() == CLONE]
    assert bad[0]["category"] == po.CAT_POISONING, bad[0]
    assert "AI inference" != bad[0]["category"]
    print("poisoning outranks registry identification OK")


def test_hard_stop_in_exception_queue():
    evs = [_ev(REAL, 0.01, i) for i in range(5)] + [_ev(CLONE, 1.88, 99)]
    rows = enrich(evs, {}, {}, poison_map=po.detect_poisoning(evs))
    exc = exceptions(rows)
    hard = [e for e in exc if "HARD STOP" in e["reason"]]
    assert len(hard) == 1, exc
    assert "Not supplier expense" in hard[0]["reason"]
    g = grouped_exceptions(rows)
    assert any("HARD STOP" in x["reason"] for x in g), g
    print("poisoning is a hard stop in exception queue OK")


def test_poisoning_beats_shape_classification():
    from counterralib import shapes as sh
    evs = [_ev(REAL, 0.001, i) for i in range(30)] + [_ev(CLONE, 1.88, 999)]
    f = po.detect_poisoning(evs)
    rows = enrich(evs, {}, {}, shape_map=sh.shape_map(evs), poison_map=f)
    bad = [r for r in rows if r["payee_wallet"].lower() == CLONE]
    assert bad[0]["category"] == po.CAT_POISONING
    print("poisoning beats shape classification OK")


def test_no_poison_map_leaves_behaviour_unchanged():
    evs = [_ev(CLONE, 1.88, 0)]
    rows = enrich(evs, {}, {})
    assert rows[0]["category"] == "Uncategorized"
    print("omitting poison_map unchanged OK")


# ---------------------------------------------------- counterfeit tokens --

def test_homoglyph_usdc_detected():
    # Cyrillic С (U+0421) standing in for Latin C
    fake_symbol = "USD\u0421"
    attacker = "0x" + "de" * 20
    assert po.is_counterfeit_token(fake_symbol, attacker)
    assert "counterfeit token" in po.counterfeit_reason(fake_symbol, attacker)
    print("homoglyph USDC detected OK")


def test_fullwidth_and_case_variants_detected():
    attacker = "0x" + "ad" * 20
    for sym in ("ＵＳＤＣ", "usdc", "UsDc"):
        assert po.is_counterfeit_token(sym, attacker), sym
    print("fullwidth/case USDC variants detected OK")


def test_canonical_usdc_not_flagged():
    assert not po.is_counterfeit_token(
        "USDC", "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913")
    assert not po.is_counterfeit_token(
        "USDC", "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v")
    print("canonical USDC contracts not flagged OK")


def test_unrelated_token_symbol_ignored():
    assert not po.is_counterfeit_token("WIF", "0x" + "aa" * 20)
    assert not po.is_counterfeit_token("", "0x" + "aa" * 20)
    print("unrelated token symbols ignored OK")


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
    print("\nALL POISON TESTS PASSED")
