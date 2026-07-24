"""
Observed-settlement evidence for the Counterra seller registry.

Counterra's registry already carries two evidence types for a payTo mapping:

  catalog      "this payTo appears in a discovery catalog under example.com/*"
  builder code "settlements to this wallet carry ERC-8021 app code bc_xxxxxxxx"

Both are SUPPLY-side: they establish that a seller exists and claims an address.
Neither says whether anyone is actually buying. This module adds the third,
DEMAND-side tier, which is only computable from an accumulated settlement
ledger — so no discovery catalog, receipt scheme or verification harness can
produce it:

  observed     "seen in N settlements from M distinct payers over D days"

Design note on why distinct payers lead, borrowed from the x402-receipts
project's sybil analysis: raw settlement count is trivially inflatable by a
seller paying itself from wallets it controls, so a wallet with 10,000
settlements from 3 payers is a weaker demand signal than one with 50
settlements from 50 payers. Counts are reported, but distinct payers are the
headline and the sort key.

FRAMING RULE (deliberate, not incidental): this is a positive badge sellers
earn, never a negative mark. `observed_evidence()` returns None rather than
"no demand" for unobserved wallets, and the registry annotation omits the field
entirely rather than writing a zero. The registry's growth depends on sellers
WANTING to be listed; a list that publicly embarrasses low-traffic sellers
stops growing with their labour.

HONEST LIMIT, which callers must not paper over: the ledger samples facilitator
wallets (a handful, at a bounded depth per run) out of the many a facilitator
network rotates through. Absence of observed settlements is therefore weak
evidence of absent demand, never proof. Every helper here reports the window
it actually saw, so a reader can judge coverage for themselves.
"""

from __future__ import annotations

import os
from typing import Dict, List, Optional

DEFAULT_DB = os.path.join("out", "counterra.db")


def _load_events(db_path=None, chain=None):
    """Accumulated events, or [] when no ledger exists yet."""
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


def ledger_window(db_path=None, chain=None):
    """
    The span the ledger actually covers, so coverage can be judged.

    Returns {'events', 'first_ts', 'last_ts', 'days'} or None for an empty ledger.
    """
    evs = _load_events(db_path, chain)
    if not evs:
        return None
    ts = sorted(e.ts for e in evs)
    days = max(1, (ts[-1] - ts[0]).days + 1)
    return {"events": len(evs),
            "first_ts": ts[0].isoformat(),
            "last_ts": ts[-1].isoformat(),
            "days": days}


def infer_chain(wallet):
    """
    Best-effort chain of a payTo address from its format.

    EVM addresses are 0x + 40 hex chars; Solana addresses are base58 and never
    start with 0x. Returns "base", "solana", or None when unrecognised.
    """
    w = (wallet or "").strip()
    if w.lower().startswith("0x") and len(w) == 42:
        return "base"
    if 32 <= len(w) <= 44 and not w.startswith("0x"):
        return "solana"
    return None


def chains_covered(db_path=None):
    """
    Which chains the ledger actually holds data for, and how much.

    Needed so that an unobserved seller on a chain we have never swept is
    reported as missing COVERAGE rather than missing demand — the two mean
    very different things and conflating them would misrepresent sellers.
    """
    out = {}
    for e in _load_events(db_path, None):
        out[e.chain] = out.get(e.chain, 0) + 1
    return out


def observed_stats(wallet, db_path=None, chain=None):
    """
    Demand stats for one payTo wallet, or None if never observed.

    Keys: settlements, distinct_payers, amount_usdc, first_ts, last_ts, days_active.
    """
    w = (wallet or "").lower()
    hits = [e for e in _load_events(db_path, chain) if e.payee_wallet.lower() == w]
    if not hits:
        return None
    ts = sorted(e.ts for e in hits)
    return {
        "settlements": len(hits),
        "distinct_payers": len({e.payer_wallet.lower() for e in hits}),
        "amount_usdc": round(sum(e.amount_usdc for e in hits), 6),
        "first_ts": ts[0].isoformat(),
        "last_ts": ts[-1].isoformat(),
        "days_active": len({t.date() for t in ts}),
    }


def observed_evidence(wallet, db_path=None, chain=None):
    """
    Registry evidence string for observed demand, or None when unobserved.

    Returns None (never a "no demand" string) by design — see FRAMING RULE.
    """
    s = observed_stats(wallet, db_path, chain)
    if not s:
        return None
    plural = "s" if s["settlements"] != 1 else ""
    pl_pay = "s" if s["distinct_payers"] != 1 else ""
    return (f"Observed demand: {s['settlements']} settlement{plural} from "
            f"{s['distinct_payers']} distinct payer{pl_pay}, "
            f"${s['amount_usdc']:.6f} total, active on {s['days_active']} day(s) "
            f"(decoded from our own accumulated ledger)")


def annotate_registry(providers, db_path=None, chain=None):
    """
    Attach observed-demand stats to registry entries.

    Accepts the registry as a list of entries or a {wallet: {...}} map, and
    returns a list of dicts sorted by distinct payers (then amount), highest
    first. Unobserved sellers are included with observed=None and no zeroed
    fields — they are simply not yet seen, which is not a demerit.
    """
    entries = []
    if isinstance(providers, dict):
        for w, v in providers.items():
            e = {"wallet": w}
            if isinstance(v, dict):
                e["label"] = v.get("label")
                e["category"] = v.get("category")
            entries.append(e)
    elif isinstance(providers, list):
        for p in providers:
            if isinstance(p, dict) and p.get("wallet"):
                entries.append({"wallet": p["wallet"],
                                "label": p.get("label"),
                                "category": p.get("category")})

    out = []
    for e in entries:
        stats = observed_stats(e["wallet"], db_path, chain)
        row = dict(e)
        row["observed"] = stats
        row["evidence"] = observed_evidence(e["wallet"], db_path, chain)
        out.append(row)

    def sort_key(r):
        s = r["observed"]
        return (-(s["distinct_payers"] if s else 0),
                -(s["amount_usdc"] if s else 0.0))
    out.sort(key=sort_key)
    return out


def demand_concentration(db_path=None, chain=None):
    """
    How concentrated observed demand is across all payees in the ledger.

    A single seller taking most settlements means the ledger's shape is driven
    by one relationship, which is worth stating plainly whenever ledger-derived
    numbers get quoted publicly.

    Returns {'payees', 'top_share_settlements', 'top_payee', 'total_settlements'}
    or None for an empty ledger.
    """
    evs = _load_events(db_path, chain)
    if not evs:
        return None
    counts = {}
    for e in evs:
        w = e.payee_wallet.lower()
        counts[w] = counts.get(w, 0) + 1
    top_payee, top_n = max(counts.items(), key=lambda kv: kv[1])
    return {"payees": len(counts),
            "total_settlements": len(evs),
            "top_payee": top_payee,
            "top_share_settlements": round(top_n / len(evs), 4)}
