"""
Address-poisoning detection.

Found via a design partner's real ledger: a $1.88 settlement that Counterra
booked as ordinary supplier expense was in fact a theft. The recipient was a
vanity clone of the partner's OWN spend wallet - an address-poisoning plant.
The partner's history also carried ~45 fake-USDC (homoglyph token) transfers
mimicking their real counterparties.

That is not an incomplete category, it is a WRONG one. A poisoning loss is
not cost of services; it is a loss, and booking it as expense understates
losses while overstating COGS. So these settlements must never post as
expense - they hard-stop for human confirmation, the same treatment CAAP-1
gives a tampered receipt.

HOW THE ATTACK WORKS: wallet UIs abbreviate addresses (0xabcd...1234), so an
attacker generates a vanity address sharing the first and last few hex
characters of a counterparty you really pay, then sends you a dust transfer
so it appears in your history. Later, you (or your agent) copy the address
from history and pay the clone instead of the real vendor. Agent wallets
making thousands of micro-payments are the ideal target: high transaction
volume, abbreviated displays, little per-payment scrutiny.

WHY THIS IS DETECTABLE HERE AND NOT GENERICALLY: the check needs to know
which counterparties are LEGITIMATELY yours. Counterra holds that map -
from the registry and from the partner's own settlement history. A generic
chain scanner cannot run this check because it does not know whose
counterparties are whose.

FALSE-POSITIVE MATH: two independent 40-hex addresses share their first 4
AND last 4 characters with probability 16^-8, about 1 in 4.3 billion. A
prefix-and-suffix match is therefore not coincidence - it is construction.
"""

from __future__ import annotations

import unicodedata
from typing import Dict, List, Optional, Set

# A wallet paid at least this many times is treated as an established,
# legitimate counterparty - and therefore as something worth impersonating.
ESTABLISHED_MIN_SETTLEMENTS = 3

# Confidence thresholds on abbreviated-display collision.
STRONG_PREFIX = 4
STRONG_SUFFIX = 4
SINGLE_END_RUN = 6

CAT_POISONING = "Suspected address poisoning - do not book as expense"
POISONING_ACCOUNT = "7910 - Suspected Misdirected Payments (under review)"

# Canonical stablecoin contracts. A token whose symbol normalises to one of
# these names but whose contract is not the canonical one is a counterfeit.
CANONICAL_TOKENS = {
    "usdc": {"0x833589fcd6edb6e08f4c7c32d4f71b54bda02913",   # Base
             "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48",   # Ethereum
             "0x036cbd53842c5426634e7929541ec2318f3dcf7e",   # Base Sepolia
             "epjfwdd5aufqssqem2qn1xzybapc8g4weggkzwytdt1v"},  # Solana
    "usdt": {"0xdac17f958d2ee523a2206206994597c13d831ec7"},
    "eurc": {"0x60a3e35cc302bfa44cb288bc5a4f316fdb1adb42"},
    "weth": {"0x4200000000000000000000000000000000000006"},
    "dai": {"0x50c5725949a6f0c72e6c4a641f24049a917db0cb"},
}


def _norm_addr(a):
    a = str(a or "")
    return a.lower() if a.startswith("0x") else a


def _body(a):
    """Address without the 0x prefix, lowercased."""
    a = _norm_addr(a)
    return a[2:] if a.startswith("0x") else a


def _run(a, b):
    """Length of the common prefix of two strings."""
    n = 0
    for x, y in zip(a, b):
        if x != y:
            break
        n += 1
    return n


def lookalike_score(candidate, target):
    """
    How much `candidate` mimics `target` in an abbreviated display.

    Returns {prefix, suffix, confidence} where confidence is "high",
    "medium" or None. Identical addresses score None - they are the same
    wallet, not an impersonation.
    """
    c, t = _body(candidate), _body(target)
    if not c or not t or c == t:
        return {"prefix": 0, "suffix": 0, "confidence": None}
    # both must be the same address family to be confusable at a glance
    if (str(candidate).startswith("0x")) != (str(target).startswith("0x")):
        return {"prefix": 0, "suffix": 0, "confidence": None}
    pre = _run(c, t)
    suf = _run(c[::-1], t[::-1])
    conf = None
    if pre >= STRONG_PREFIX and suf >= STRONG_SUFFIX:
        conf = "high"
    elif pre >= SINGLE_END_RUN or suf >= SINGLE_END_RUN:
        conf = "medium"
    return {"prefix": pre, "suffix": suf, "confidence": conf}


def established_counterparties(events, registry_wallets=None,
                               min_settlements=ESTABLISHED_MIN_SETTLEMENTS):
    """
    The set of wallets that are legitimately this operator's counterparties:
    anything in the registry, plus any wallet settled with repeatedly.

    Also includes the operator's OWN payer wallets - the partner's case shows
    attackers clone the spend wallet itself, not only its vendors.
    """
    counts = {}
    payers = set()
    for e in events:
        counts[_norm_addr(e.payee_wallet)] = counts.get(
            _norm_addr(e.payee_wallet), 0) + 1
        payers.add(_norm_addr(e.payer_wallet))
    out = {w for w, n in counts.items() if n >= min_settlements}
    out |= payers
    out |= {_norm_addr(w) for w in (registry_wallets or [])}
    return out


def detect_poisoning(events, established=None, registry_wallets=None):
    """
    Flag settlements whose PAYEE impersonates an established counterparty.

    Returns {wallet_lower: finding}, finding = {category, label, confidence,
    rationale, impersonates, settlements, amount_usd}.
    """
    est = set(established if established is not None
              else established_counterparties(events, registry_wallets))
    # aggregate per payee
    agg = {}
    for e in events:
        w = _norm_addr(e.payee_wallet)
        if w not in agg:
            agg[w] = {"n": 0, "usd": 0.0}
        agg[w]["n"] += 1
        agg[w]["usd"] += float(e.amount_usdc)

    findings = {}
    for w, a in agg.items():
        if w in est:
            continue                      # an established wallet is not a clone
        best = None
        for target in est:
            s = lookalike_score(w, target)
            if not s["confidence"]:
                continue
            rank = (s["prefix"] + s["suffix"], s["confidence"] == "high")
            if best is None or rank > best[0]:
                best = (rank, target, s)
        if not best:
            continue
        _, target, s = best
        # Dust-and-single-payment makes the case stronger still.
        dust = a["n"] <= 2
        conf = s["confidence"]
        if conf == "high" and dust:
            conf = "high"
        elif conf == "medium" and not dust:
            conf = "low"
        findings[w] = {
            "category": CAT_POISONING,
            "label": "Suspected poisoning clone " + w[:10] + "…",
            "confidence": conf,
            "impersonates": target,
            "settlements": a["n"],
            "amount_usd": round(a["usd"], 6),
            "rationale": (
                "payee shares first {} and last {} hex characters with "
                "established counterparty {}…{} but is a different address "
                "({} settlement(s), ${:.2f}) - consistent with an "
                "address-poisoning clone copied from transaction history "
                "rather than a real vendor".format(
                    s["prefix"], s["suffix"], target[:10], target[-6:],
                    a["n"], a["usd"])),
        }
    return findings


# ----------------------------------------------------------- fake tokens --

# Explicit homoglyph table. Deriving these from Unicode character names does
# not work - Cyrillic С is "CYRILLIC CAPITAL LETTER ES", which yields no
# single-letter skeleton - so the confusable pairs used in ticker spoofing are
# listed outright. Cyrillic and Greek cover the overwhelming majority of
# real-world stablecoin symbol impersonation.
CONFUSABLES = {
    # Cyrillic -> Latin
    "\u0410": "a", "\u0412": "b", "\u0421": "c", "\u0415": "e",
    "\u041d": "h", "\u041a": "k", "\u041c": "m", "\u041e": "o",
    "\u0420": "p", "\u0422": "t", "\u0425": "x", "\u0405": "s",
    "\u0406": "i", "\u0408": "j",
    "\u0430": "a", "\u0432": "b", "\u0441": "c", "\u0435": "e",
    "\u043d": "h", "\u043a": "k", "\u043c": "m", "\u043e": "o",
    "\u0440": "p", "\u0442": "t", "\u0445": "x", "\u0455": "s",
    "\u0456": "i", "\u0443": "y", "\u0434": "d",
    # Greek -> Latin
    "\u0391": "a", "\u0392": "b", "\u0395": "e", "\u0396": "z",
    "\u0397": "h", "\u0399": "i", "\u039a": "k", "\u039c": "m",
    "\u039d": "n", "\u039f": "o", "\u03a1": "p", "\u03a4": "t",
    "\u03a5": "y", "\u03a7": "x", "\u03bf": "o", "\u03c1": "p",
    "\u03c5": "u", "\u03b1": "a", "\u03b5": "e",
    # Armenian / other frequent stand-ins
    "\u0555": "o", "\u054f": "s", "\u0570": "h",
}


def _normalize_symbol(sym):
    """
    Fold a token symbol to bare ASCII lowercase so homoglyph, full-width and
    case impersonations of a real ticker collapse onto the same string.
    """
    s = unicodedata.normalize("NFKC", str(sym or ""))
    out = []
    for ch in s:
        if ch.isascii() and ch.isalnum():
            out.append(ch.lower())
        elif ch in CONFUSABLES:
            out.append(CONFUSABLES[ch])
        elif ch.lower() in CONFUSABLES:
            out.append(CONFUSABLES[ch.lower()])
    return "".join(out)


def is_counterfeit_token(symbol, contract):
    """
    True when a token's symbol impersonates a canonical stablecoin but its
    contract is not the canonical one. This is the ~45 fake-USDC events the
    design partner saw: real-looking symbol, attacker-controlled contract.
    """
    norm = _normalize_symbol(symbol)
    if norm not in CANONICAL_TOKENS:
        return False
    return str(contract or "").lower() not in CANONICAL_TOKENS[norm]


def counterfeit_reason(symbol, contract):
    norm = _normalize_symbol(symbol)
    return ("token symbol {!r} normalises to '{}' but contract {} is not a "
            "canonical {} contract - counterfeit token, not real value "
            "received".format(symbol, norm, _norm_addr(contract)[:14] + "…",
                              norm.upper()))
