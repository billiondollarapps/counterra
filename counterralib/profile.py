"""
Seller fingerprinting from settlement patterns.

Some payTo wallets cannot be identified by the two existing routes: they are
absent from every discovery catalog, and they carry no ERC-8021 builder code.
That leaves manual investigation — but a wallet's settlement pattern is not
blank. It carries structure that narrows the search before anyone opens a block
explorer:

  price ladder     distinct amounts charged. A wallet billing exactly
                   $0.003 / $1 / $5 / $15 is running tiered endpoints, not a
                   single flat-rate API; the tier count is roughly a lower
                   bound on how many products it sells.
  payer overlap    whether its buyers also pay sellers we HAVE identified. An
                   agent buying market data from a known seller and also paying
                   an unknown one is a category hint. Zero overlap means a
                   distinct buyer population, which is itself informative.
  payer exclusivity  share of its payers that pay nobody else we can see -
                   high exclusivity suggests a dedicated integration rather
                   than incidental purchases by generalist agents.
  cadence          gaps between settlements. Tight, regular gaps suggest
                   scheduled/automated buying; irregular bursts suggest
                   human-triggered or event-driven use.

HONEST SCOPE: none of this identifies anyone. It produces hints that rank and
direct manual work, and every hint is derived from a bounded sample of
facilitator sweeps. Treat outputs as leads, never conclusions - the functions
return plain observations and deliberately avoid asserting a seller's identity.
"""

from __future__ import annotations

import os
from collections import Counter
from typing import Dict, List, Optional

DEFAULT_DB = os.path.join("out", "counterra.db")


def _load_events(db_path=None, chain=None):
    path = db_path or DEFAULT_DB
    if not os.path.exists(path):
        return []
    try:
        from counterralib.store import EventStore
        store = EventStore(path)
        try:
            return store.all_events(chain)
        finally:
            store.close()
    except Exception:
        return []


def _known_map(providers):
    """Normalise a registry into {wallet_lower: label}."""
    out = {}
    if isinstance(providers, dict):
        for w, v in providers.items():
            out[w.lower()] = (v.get("label") if isinstance(v, dict) else str(v))
    elif isinstance(providers, list):
        for p in providers:
            if isinstance(p, dict) and p.get("wallet"):
                out[p["wallet"].lower()] = p.get("label")
    return out


def price_ladder(settlements):
    """
    Distinct amounts charged, most frequent first.

    Returns a list of (amount, count). Distinct price points are the clearest
    signal that a wallet fronts several priced endpoints rather than one.
    """
    c = Counter(round(e.amount_usdc, 6) for e in settlements)
    return sorted(c.items(), key=lambda kv: (-kv[1], -kv[0]))


def cadence(settlements):
    """
    Gaps between consecutive settlements, in seconds.

    Returns {'median_gap_s', 'min_gap_s', 'max_gap_s'} or None when there are
    fewer than two settlements to compare.
    """
    ts = sorted(e.ts for e in settlements)
    if len(ts) < 2:
        return None
    gaps = [(ts[i + 1] - ts[i]).total_seconds() for i in range(len(ts) - 1)]
    gaps.sort()
    mid = len(gaps) // 2
    median = gaps[mid] if len(gaps) % 2 else (gaps[mid - 1] + gaps[mid]) / 2
    return {"median_gap_s": round(median, 1),
            "min_gap_s": round(gaps[0], 1),
            "max_gap_s": round(gaps[-1], 1)}


def profile_wallet(wallet, providers=None, db_path=None, chain=None):
    """
    Build an investigative profile of one payTo wallet, or None if unseen.

    Returns observations only - settlements, payers, price ladder, cadence,
    payer overlap with known sellers, payer exclusivity, and a list of
    plain-language notes. No identity is asserted.
    """
    w = (wallet or "").lower()
    evs = _load_events(db_path, chain)
    mine = [e for e in evs if e.payee_wallet.lower() == w]
    if not mine:
        return None

    known = _known_map(providers or {})
    my_payers = {e.payer_wallet.lower() for e in mine}

    # Which identified sellers do this wallet's payers also buy from?
    overlap = Counter()
    payers_seen_elsewhere = set()
    for e in evs:
        p = e.payer_wallet.lower()
        if p not in my_payers:
            continue
        other = e.payee_wallet.lower()
        if other == w:
            continue
        payers_seen_elsewhere.add(p)
        if other in known and known[other]:
            overlap[known[other]] += 1

    ladder = price_ladder(mine)
    ts = sorted(e.ts for e in mine)
    exclusive = len(my_payers - payers_seen_elsewhere)

    prof = {
        "wallet": wallet,
        "settlements": len(mine),
        "distinct_payers": len(my_payers),
        "amount_usdc": round(sum(e.amount_usdc for e in mine), 6),
        "first_ts": ts[0].isoformat(),
        "last_ts": ts[-1].isoformat(),
        "price_ladder": ladder,
        "tier_count": len(ladder),
        "max_price": max(a for a, _ in ladder),
        "min_price": min(a for a, _ in ladder),
        "cadence": cadence(mine),
        "payer_overlap": overlap.most_common(),
        "exclusive_payers": exclusive,
        "exclusivity": round(exclusive / len(my_payers), 3) if my_payers else 0.0,
        "builder_codes": sorted({e.app_code for e in mine if e.app_code}),
        "memos": [m for m, _ in Counter(e.memo for e in mine if e.memo).most_common(3)],
        "notes": [],
    }

    n = prof["notes"]
    if prof["tier_count"] >= 3:
        n.append(f"{prof['tier_count']} distinct price points "
                 f"(${prof['min_price']:g}-${prof['max_price']:g}) - looks like tiered "
                 f"endpoints rather than one flat-rate API")
    elif prof["tier_count"] == 1:
        n.append(f"single price point (${prof['min_price']:g}) - likely one endpoint "
                 f"or uniform per-call pricing")
    if prof["max_price"] >= 1.0:
        n.append(f"top tier is ${prof['max_price']:g}, far above the sub-cent norm - "
                 f"suggests a substantive product (report, bulk data, compute), "
                 f"not a per-call API")
    if prof["distinct_payers"] >= 5 and prof["settlements"] <= prof["distinct_payers"] * 2:
        n.append(f"{prof['distinct_payers']} distinct payers with few repeat purchases - "
                 f"broad trial interest rather than one embedded integration")
    if prof["exclusivity"] >= 0.8 and prof["distinct_payers"] >= 3:
        n.append(f"{int(prof['exclusivity']*100)}% of its payers buy from no other seller "
                 f"we can see - a distinct buyer population, so category cannot be "
                 f"inferred from overlap")
    if overlap:
        top = ", ".join(f"{k} ({v})" for k, v in overlap.most_common(3))
        n.append(f"its payers also buy from: {top} - possible category neighbours")
    if not prof["builder_codes"]:
        n.append("no ERC-8021 builder code on any settlement - not self-identifying yet")
    return prof


def unidentified_ranked(providers=None, db_path=None, chain=None, limit=10):
    """
    Unregistered payTo wallets ranked by how much they matter to the books.

    Ranked by value first, then distinct payers, so the wallet whose absence
    distorts the accounts most is investigated first.
    """
    known = _known_map(providers or {})
    agg = {}
    for e in _load_events(db_path, chain):
        w = e.payee_wallet.lower()
        if w in known:
            continue
        a = agg.setdefault(w, {"wallet": e.payee_wallet, "settlements": 0,
                               "amount_usdc": 0.0, "_payers": set()})
        a["settlements"] += 1
        a["amount_usdc"] += e.amount_usdc
        a["_payers"].add(e.payer_wallet.lower())
    out = []
    for a in agg.values():
        a["distinct_payers"] = len(a.pop("_payers"))
        a["amount_usdc"] = round(a["amount_usdc"], 6)
        out.append(a)
    out.sort(key=lambda a: (-a["amount_usdc"], -a["distinct_payers"]))
    return out[:limit]
