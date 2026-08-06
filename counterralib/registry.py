"""
Registry plumbing for Counterra's self-growing seller map.

The public registry (docs/providers.json) is the asset. This module gives it
proper machinery so it can grow unattended WITHOUT unverified data ever
entering it:

  VERIFIED   - mechanical evidence (catalog payTo match, live 402 resolution,
               a Basename the owner registered, a verified contract name).
               Safe to auto-append to docs/providers.json.
  PROBABLE   - circumstantial evidence (Blockscout public tags, an address
               found in web text). Queued in docs/pending-providers.json for
               a human to approve or reject. NEVER auto-published.

Also home to `unknown_sellers`: rank every unmapped payee in the accumulated
EventStore by dollar volume, so identification effort always follows the
Pareto curve (~157 addresses hold ~82% of x402 revenue - identify by volume,
not by count).
"""

from __future__ import annotations

import datetime
import json
import os
from typing import Dict, List, Optional, Set, Tuple

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REGISTRY_PATH = os.path.join(HERE, "docs", "providers.json")
PENDING_PATH = os.path.join(HERE, "docs", "pending-providers.json")


def _norm(addr: str) -> str:
    a = str(addr)
    return a.lower() if a.startswith("0x") else a


# ---------------------------------------------------------------- registry --

def load_registry(path: Optional[str] = None) -> dict:
    p = path or REGISTRY_PATH
    with open(p) as f:
        return json.load(f)


def save_registry(reg: dict, path: Optional[str] = None):
    p = path or REGISTRY_PATH
    with open(p, "w") as f:
        json.dump(reg, f, indent=2)
        f.write("\n")


def known_wallets(reg: dict) -> Set[str]:
    return {_norm(p["wallet"]) for p in reg.get("providers", [])}


def make_entry(wallet: str, chain: str, label: str, category: str,
               evidence: str) -> dict:
    return {
        "wallet": _norm(wallet) if chain != "solana" else wallet,
        "chain": chain,
        "label": label,
        "category": category,
        "evidence": evidence,
        "added": datetime.date.today().isoformat(),
    }


def append_verified(reg: dict, entries: List[dict]) -> List[dict]:
    """Append entries not already present. Returns the ones actually added."""
    have = known_wallets(reg)
    added = []
    for e in entries:
        if _norm(e["wallet"]) not in have:
            reg["providers"].append(e)
            have.add(_norm(e["wallet"]))
            added.append(e)
    return added


# ---------------------------------------------------------- pending queue --

def load_pending(path: Optional[str] = None) -> dict:
    p = path or PENDING_PATH
    if not os.path.exists(p):
        return {"$schema_note": ("Counterra pending-identification queue. "
                                 "Probable (non-mechanical) evidence waits "
                                 "here for human review; nothing in this file "
                                 "is published. Approve with: "
                                 "counterra.py pending --approve N"),
                "version": 1, "pending": []}
    with open(p) as f:
        return json.load(f)


def save_pending(pend: dict, path: Optional[str] = None):
    p = path or PENDING_PATH
    with open(p, "w") as f:
        json.dump(pend, f, indent=2)
        f.write("\n")


def queue_probable(pend: dict, entry: dict, confidence: str,
                   already_known: Set[str]) -> bool:
    """Add a probable candidate unless it's already known or already queued.
    Returns True if queued."""
    w = _norm(entry["wallet"])
    if w in already_known:
        return False
    for p in pend["pending"]:
        if _norm(p["wallet"]) == w:
            return False
    q = dict(entry)
    q["confidence"] = confidence
    pend["pending"].append(q)
    return True


def approve_pending(pend: dict, reg: dict, index: int) -> Optional[dict]:
    """Move pending[index] (1-based) into the registry. Returns the entry."""
    i = index - 1
    if i < 0 or i >= len(pend["pending"]):
        return None
    entry = pend["pending"].pop(i)
    entry.pop("confidence", None)
    added = append_verified(reg, [entry])
    return added[0] if added else entry


def reject_pending(pend: dict, index: int) -> Optional[dict]:
    i = index - 1
    if i < 0 or i >= len(pend["pending"]):
        return None
    return pend["pending"].pop(i)


# ------------------------------------------------- unknown-seller ranking --

def unknown_sellers(events, known: Set[str],
                    chain: Optional[str] = None) -> List[Tuple[str, str, float, int]]:
    """
    Rank unmapped payees in a list of PaymentEvents by dollar volume.

    Returns [(payee_wallet, chain, total_usd, n_settlements)], largest first.
    Volume-ranked so autogrow always spends its lookup budget where the
    money is, never on the long tail.
    """
    agg: Dict[Tuple[str, str], List[float]] = {}
    for e in events:
        if chain and e.chain != chain:
            continue
        w = _norm(e.payee_wallet)
        if w in known:
            continue
        key = (w, e.chain)
        if key not in agg:
            agg[key] = [0.0, 0]
        agg[key][0] += float(e.amount_usdc)
        agg[key][1] += 1
    out = [(w, c, round(v[0], 6), int(v[1])) for (w, c), v in agg.items()]
    out.sort(key=lambda t: -t[2])
    return out
