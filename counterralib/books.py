"""
One-command books for a design partner's wallet.

Everything else in Counterra is operator-facing: it assumes you edit config,
know which chain to pass, and read a terminal. A design partner cannot do that.
This module is the handoff surface — point it at an agent wallet and it produces
that wallet's books with no configuration:

    counterra.py books --wallet 0xABC...

It auto-detects the chain from the address format, sweeps that chain for the
wallet's outgoing x402 spend, runs the same enrich -> journal -> report pipeline
the operator commands use, and writes a labelled HTML report plus QuickBooks/Xero
exports the partner can hand to their accountant. No config edits, no chain flag,
no ledger knowledge required.

Design intent: the gap between "impressive repo" and "someone who isn't the
author can use it" is the gap between now and a paying customer. This closes it.
"""

from __future__ import annotations

import re
from typing import Optional


def detect_chain(wallet):
    """
    Infer the chain from a wallet's address format.

    EVM (Base) addresses are 0x + 40 hex; Solana addresses are base58, 32-44
    chars, no 0x prefix. Returns "base", "solana", or None if unrecognisable —
    so a partner never has to know or pass a chain flag.
    """
    w = (wallet or "").strip()
    if re.fullmatch(r"0x[0-9a-fA-F]{40}", w):
        return "base"
    if re.fullmatch(r"[1-9A-HJ-NP-Za-km-z]{32,44}", w) and not w.startswith("0x"):
        return "solana"
    return None


def build_books(wallet, cfg, limit=500):
    """
    Fetch a wallet's x402 spend and return (events, chain, warnings).

    Uses the same adapters the `live` command uses, but wrapped so the caller
    needs no chain knowledge. Never raises on an empty result — a wallet with no
    spend is a valid, bookable answer (empty books), not an error.
    """
    warnings = []
    chain = detect_chain(wallet)
    if chain is None:
        return [], None, [f"Could not recognise '{wallet}' as a Base (0x...) or "
                          f"Solana address."]

    if chain == "solana":
        from counterralib.solana import SolanaChainAdapter
        adapter = SolanaChainAdapter(cfg)
    else:
        from counterralib.live import BaseChainAdapter
        adapter = BaseChainAdapter(cfg, quiet=True)

    try:
        events = adapter.fetch_wallet(wallet, limit=limit)
    except Exception as e:
        return [], chain, [f"Could not fetch {chain} data for this wallet: {e}"]

    if not events:
        warnings.append("No x402 spend found for this wallet in the swept window. "
                        "The books are empty, which is a valid result — this wallet "
                        "may not have paid over x402 yet, or activity is outside the "
                        "sweep depth.")
    return events, chain, warnings


def partner_summary(events, chain):
    """A short, human summary line for a design partner (not operator jargon)."""
    if not events:
        return f"No x402 spend found on {chain.title()} for this wallet."
    total = sum(e.amount_usdc for e in events)
    sellers = len({e.payee_wallet.lower() for e in events})
    return (f"{len(events)} payments on {chain.title()}, ${total:,.4f} total, "
            f"across {sellers} seller(s).")


def load_receipts(folder):
    """
    Load x402 receipts from a folder of .json files, keyed by settlement tx_hash.

    A design partner exports their receipts (one JSON per payment, or a JSON
    array) into a folder and points --receipts at it. Best-effort: unreadable
    files are skipped, never fatal.
    """
    import glob
    import json
    out = {}
    if not folder or not os.path.isdir(folder):
        return out
    for path in glob.glob(os.path.join(folder, "*.json")):
        try:
            data = json.load(open(path))
        except Exception:
            continue
        items = data if isinstance(data, list) else [data]
        for r in items:
            if not isinstance(r, dict):
                continue
            tx = (((r.get("payment") or {}).get("tx_hash")) or "").lower()
            if tx:
                out[tx] = r
    return out


def enrich_events_with_receipts(events, receipts_by_tx):
    """
    Attach receipt delivery context to swept PaymentEvents by tx_hash.

    The settlement stays the source of truth for amount; the receipt only adds
    the 'what / whether-delivered' the chain can't show. Returns (events,
    matched_count).
    """
    if not receipts_by_tx:
        return events, 0
    matched = 0
    for e in events:
        r = receipts_by_tx.get(str(e.tx_hash).lower())
        if not r:
            continue
        matched += 1
        goods = r.get("goods") or {}
        delivery = (r.get("delivery") or {}).get("status", "delivered")
        resp = r.get("response") or {}
        desc = goods.get("description") or goods.get("kind") or ""
        e.memo = (f"{(r.get('request') or {}).get('method','')} {desc} "
                  f"[{delivery}, HTTP {resp.get('status','?')}]").strip()
    return events, matched
