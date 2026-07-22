"""
Builder-code evidence for the Counterra seller registry.

Counterra decodes ERC-8021 attribution suffixes from x402 settlement calldata
(see erc8021.py). Those suffixes can carry an app code ('a') identifying the
application that served the paid endpoint — i.e. the SELLER. That makes builder
codes a second, independent evidence source for registry entries, alongside
discovery-catalog payTo matching:

  catalog evidence : "this payTo appears in the Bazaar under example.com/*"
  builder-code ev. : "settlements to this wallet carry app code bc_xxxxxxxx"

The two corroborate each other. Where they disagree, that is worth surfacing
rather than silently trusting one — hence conflict detection below.

A third use falls out for free: if the SAME builder code appears on several
different payTo wallets, those wallets are very likely the same operator, so
they can be clustered in the registry. No other seller catalog can do this,
because it requires decoding settlement calldata rather than reading listings.

All functions degrade gracefully when no ledger exists yet (fresh clone,
no accumulated events) — they return empty results rather than raising.
"""

from __future__ import annotations

import os
from collections import defaultdict
from typing import Dict, List, Optional

DEFAULT_DB = os.path.join("out", "counterra.db")


def _load_events(db_path=None, chain=None):
    """Load accumulated events, or [] if the ledger doesn't exist / can't open."""
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


def codes_for_wallet(wallet, db_path=None, chain=None):
    """
    All ERC-8021 app codes observed on settlements PAID TO this wallet.

    Returns {code: settlement_count}, highest count first. Empty if none.
    """
    w = (wallet or "").lower()
    counts = defaultdict(int)
    for e in _load_events(db_path, chain):
        if e.payee_wallet.lower() == w and e.app_code:
            counts[e.app_code] += 1
    return dict(sorted(counts.items(), key=lambda kv: -kv[1]))


def wallets_for_code(code, db_path=None, chain=None):
    """
    All payee wallets seen carrying a given app code.

    More than one wallet for the same code means one operator is settling to
    several addresses — a cluster the registry can record as one seller.
    """
    out = defaultdict(int)
    for e in _load_events(db_path, chain):
        if e.app_code == code:
            out[e.payee_wallet.lower()] += 1
    return dict(sorted(out.items(), key=lambda kv: -kv[1]))


def builder_code_evidence(wallet, db_path=None, chain=None):
    """
    Human-readable registry evidence string from builder codes, or None.

    Example:
      "ERC-8021 builder code bc_qobj93ib on 9 settlements to this wallet
       (decoded from settlement calldata)"
    """
    codes = codes_for_wallet(wallet, db_path, chain)
    if not codes:
        return None
    parts = [f"{c} on {n} settlement{'s' if n != 1 else ''}"
             for c, n in codes.items()]
    return ("ERC-8021 builder code " + "; ".join(parts) +
            " (decoded from settlement calldata)")


def detect_conflicts(providers, db_path=None, chain=None):
    """
    Cross-check registry entries against observed builder codes.

    Flags two kinds of disagreement worth a human look:
      - 'shared_code': one builder code appears on wallets the registry
        attributes to DIFFERENT sellers (either a mislabel, or one operator
        running several brands/wallets).
      - 'multi_code': one wallet carries several different app codes
        (possible shared infrastructure, or a relabelled endpoint).

    `providers` is the registry map {wallet: {label, category}} or a list of
    registry entries. Returns a list of dicts; empty list when all consistent.
    """
    # Normalise providers into {wallet_lower: label}
    labels = {}
    if isinstance(providers, dict):
        for w, v in providers.items():
            labels[w.lower()] = (v.get("label") if isinstance(v, dict)
                                 else (v[0] if isinstance(v, (list, tuple)) and v else str(v)))
    elif isinstance(providers, list):
        for p in providers:
            if isinstance(p, dict) and p.get("wallet"):
                labels[p["wallet"].lower()] = p.get("label")

    events = _load_events(db_path, chain)
    code_to_wallets = defaultdict(set)
    wallet_to_codes = defaultdict(set)
    for e in events:
        if e.app_code:
            code_to_wallets[e.app_code].add(e.payee_wallet.lower())
            wallet_to_codes[e.payee_wallet.lower()].add(e.app_code)

    conflicts = []
    for code, wallets in code_to_wallets.items():
        if len(wallets) > 1:
            named = {labels.get(w) for w in wallets if labels.get(w)}
            conflicts.append({
                "type": "shared_code",
                "code": code,
                "wallets": sorted(wallets),
                "registry_labels": sorted(x for x in named if x),
                "note": ("same builder code on multiple wallets - likely one "
                         "operator; consider clustering in the registry"),
            })
    for wallet, codes in wallet_to_codes.items():
        if len(codes) > 1:
            conflicts.append({
                "type": "multi_code",
                "wallet": wallet,
                "codes": sorted(codes),
                "registry_label": labels.get(wallet),
                "note": ("one wallet carrying several builder codes - shared "
                         "infrastructure or a relabelled endpoint; verify"),
            })
    return conflicts


def unregistered_with_codes(providers, db_path=None, chain=None):
    """
    Wallets that carry a builder code but are NOT yet in the registry.

    These are the highest-value classification targets: a seller that tagged
    its own settlements is self-identifying, so an entry can be proposed with
    on-chain evidence even when no discovery catalog lists it.
    """
    known = set()
    if isinstance(providers, dict):
        known = {w.lower() for w in providers}
    elif isinstance(providers, list):
        known = {p["wallet"].lower() for p in providers
                 if isinstance(p, dict) and p.get("wallet")}

    found = defaultdict(lambda: {"codes": defaultdict(int), "settlements": 0, "amount": 0.0})
    for e in _load_events(db_path, chain):
        w = e.payee_wallet.lower()
        if w in known or not e.app_code:
            continue
        found[w]["codes"][e.app_code] += 1
        found[w]["settlements"] += 1
        found[w]["amount"] += e.amount_usdc

    out = []
    for w, v in sorted(found.items(), key=lambda kv: -kv[1]["amount"]):
        out.append({
            "wallet": w,
            "codes": dict(v["codes"]),
            "settlements": v["settlements"],
            "amount_usdc": round(v["amount"], 6),
            "evidence": builder_code_evidence(w, db_path, chain),
        })
    return out
