"""
Shape-based categorisation - book the unidentifiable honestly.

Most x402 sellers will never be named: absent from every catalog, no on-chain
name, no builder code. Today those settlements all land in one bucket -
"Uncategorized (6490)" - which is the single loudest complaint a partner has
about their books. But an unnamed wallet is not a shapeless one. 545 flat
sub-cent settlements at machine cadence is a metered API whether or not anyone
knows whose it is, and an accountant can book that.

So this module separates two questions that were previously fused:

    WHO is this seller?      - often unanswerable (needs the registry)
    WHAT KIND of spend is it? - usually answerable from the settlement pattern

It answers only the second, and it is careful never to imply it answered the
first. Every label it produces starts with "Unidentified" and every category
it assigns is a distinct account from the identified equivalents, so a
reviewer can always separate "we know this is an AI inference vendor" from
"this behaves like a metered API". Shape-classified spend also STAYS in the
exception queue - better described, not swept under the rug.

Shapes, in priority order:

  metered-api        many settlements, one or two price points, all small,
                     tight machine cadence -> per-call API metering
  tiered-service     3+ distinct price points across a range -> several
                     priced endpoints behind one wallet
  recurring-charge   regular multi-hour/daily cadence at one identical
                     amount, modest volume -> subscription-like
  bulk-purchase      few settlements, large amounts -> reports, datasets,
                     compute jobs; not per-call
  (none)             too little evidence - stays Uncategorized

HONEST SCOPE: these are behavioural inferences, not identifications. A wallet
can change what it sells. Confidence is reported and low-evidence wallets are
deliberately left alone rather than guessed at.
"""

from __future__ import annotations

from collections import Counter
from typing import Dict, List, Optional

# Minimum settlements before any shape is asserted at all.
MIN_SETTLEMENTS = 4

# Category strings. Deliberately prefixed so they can never be confused with
# an identified seller's category, and so config/report code can find them.
SHAPE_PREFIX = "Unidentified - "
CAT_METERED = SHAPE_PREFIX + "metered API"
CAT_TIERED = SHAPE_PREFIX + "tiered service"
CAT_RECURRING = SHAPE_PREFIX + "recurring charge"
CAT_BULK = SHAPE_PREFIX + "bulk purchase"

SHAPE_CATEGORIES = (CAT_METERED, CAT_TIERED, CAT_RECURRING, CAT_BULK)

# Default expense accounts for shape categories. Merged into the configured
# expense_accounts map at book time, so an operator can override any of them
# in config.yaml without touching code.
SHAPE_ACCOUNTS = {
    CAT_METERED: "6480 - Unidentified Metered API Spend",
    CAT_TIERED: "6481 - Unidentified Tiered Service Spend",
    CAT_RECURRING: "6482 - Unidentified Recurring Charges",
    CAT_BULK: "6483 - Unidentified Bulk Purchases",
}


def is_shape_category(category):
    return bool(category) and str(category).startswith(SHAPE_PREFIX)


def _median(xs):
    if not xs:
        return None
    s = sorted(xs)
    mid = len(s) // 2
    return s[mid] if len(s) % 2 else (s[mid - 1] + s[mid]) / 2.0


def classify_shape(settlements):
    """
    Infer a spend shape from one wallet's settlements.

    `settlements` is a list of PaymentEvent-like objects (need .amount_usdc
    and .ts). Returns {category, label, confidence, rationale} or None when
    there is too little evidence to say anything.
    """
    n = len(settlements)
    if n < MIN_SETTLEMENTS:
        return None

    amounts = [round(float(e.amount_usdc), 6) for e in settlements]
    ladder = Counter(amounts)
    tiers = len(ladder)
    top = max(amounts)
    med = _median(amounts)

    ts = sorted(e.ts for e in settlements)
    gaps = [(ts[i + 1] - ts[i]).total_seconds() for i in range(len(ts) - 1)]
    med_gap = _median(gaps) if gaps else None

    # How often price points repeat. A published price list gets reused
    # (repeat >= 2); one-off purchases each have their own unique size
    # (repeat ~= 1). This is what separates a tiered service from bulk buying
    # when both involve dollar-scale amounts.
    repeat = float(n) / tiers

    # --- tiered service: several price points, and they RECUR -------------
    if tiers >= 3 and repeat >= 2.0 and top >= 0.01:
        return {
            "category": CAT_TIERED,
            "label": "Unidentified tiered service",
            "confidence": "medium" if n >= 10 else "low",
            "rationale": ("{} distinct price points (${:g}-${:g}) each recurring "
                          "across {} settlements - a price list behind one "
                          "wallet, not a single flat-rate API"
                          ).format(tiers, min(amounts), top, n),
        }

    # --- bulk purchase: few, large, each a different size -----------------
    if med is not None and med >= 1.0 and n <= 20 and repeat < 2.0:
        return {
            "category": CAT_BULK,
            "label": "Unidentified bulk purchase",
            "confidence": "medium" if n >= 6 else "low",
            "rationale": ("{} settlements, median ${:g} (top ${:g}), mostly "
                          "distinct amounts - suggests reports, datasets or "
                          "compute jobs rather than API metering"
                          ).format(n, med, top),
        }

    # --- recurring charge: identical amount on a regular slow cadence -----
    if (tiers == 1 and med_gap is not None and med_gap >= 3600
            and n >= MIN_SETTLEMENTS):
        hrs = med_gap / 3600.0
        return {
            "category": CAT_RECURRING,
            "label": "Unidentified recurring charge",
            "confidence": "medium" if n >= 8 else "low",
            "rationale": ("{} settlements of exactly ${:g} at a regular ~{:.1f}h "
                          "cadence - behaves like a subscription or scheduled "
                          "job, not ad-hoc calls").format(n, amounts[0], hrs),
        }

    # --- metered API: high volume, few prices, small amounts --------------
    if tiers <= 2 and top < 1.0 and n >= MIN_SETTLEMENTS:
        conf = "high" if n >= 50 else ("medium" if n >= 12 else "low")
        cad = ""
        if med_gap is not None and med_gap < 600:
            cad = ", median gap {:.0f}s (machine cadence)".format(med_gap)
        return {
            "category": CAT_METERED,
            "label": "Unidentified metered API",
            "confidence": conf,
            "rationale": ("{} settlements at {} price point(s), all under $1 "
                          "(median ${:g}){} - per-call API metering"
                          ).format(n, tiers, med, cad),
        }

    return None


def shape_map(events, known_wallets=None):
    """
    Build {wallet_lower: shape_dict} for every UNIDENTIFIED payee in `events`.

    Wallets present in `known_wallets` are skipped - a real identification
    always wins over a behavioural guess.
    """
    known = {str(w).lower() for w in (known_wallets or [])}
    by_wallet = {}
    for e in events:
        w = str(e.payee_wallet).lower()
        if w in known:
            continue
        by_wallet.setdefault(w, []).append(e)
    out = {}
    for w, evs in by_wallet.items():
        shape = classify_shape(evs)
        if shape:
            out[w] = shape
    return out


def coverage(events, shapes, known_wallets=None):
    """
    How much of the spend each bucket accounts for - the metric that actually
    matters to a partner reading their books.

    Returns {identified_usd, shaped_usd, unknown_usd, total_usd, and the same
    as settlement counts}, so a report can say "83% of spend is now booked to
    a real account" instead of just listing wallets.
    """
    known = {str(w).lower() for w in (known_wallets or [])}
    out = {"identified_usd": 0.0, "shaped_usd": 0.0, "unknown_usd": 0.0,
           "identified_n": 0, "shaped_n": 0, "unknown_n": 0}
    for e in events:
        w = str(e.payee_wallet).lower()
        amt = float(e.amount_usdc)
        if w in known:
            out["identified_usd"] += amt
            out["identified_n"] += 1
        elif w in shapes:
            out["shaped_usd"] += amt
            out["shaped_n"] += 1
        else:
            out["unknown_usd"] += amt
            out["unknown_n"] += 1
    total = out["identified_usd"] + out["shaped_usd"] + out["unknown_usd"]
    out["total_usd"] = round(total, 6)
    for k in ("identified_usd", "shaped_usd", "unknown_usd"):
        out[k] = round(out[k], 6)
    out["booked_pct"] = (round(100.0 * (out["identified_usd"] + out["shaped_usd"])
                               / total, 1) if total else 0.0)
    return out


def merged_expense_accounts(configured):
    """Configured accounts plus shape defaults (config always wins)."""
    merged = dict(SHAPE_ACCOUNTS)
    merged.update(configured or {})
    return merged
