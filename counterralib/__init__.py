"""
Counterra — financial telemetry for agent payments.

Public API. Import these; the internal module layout (counterralib.*) may change
between versions, but the functions exported here keep stable signatures. This
is the surface other tools, design partners, and the future aggregation layer
build against.

    from counterralib import books_for_wallet, analyze_ledger, receipt_to_journal

Design contract:
  - Booking is DETERMINISTIC and CAAP-1 conformant. Same inputs -> same books.
  - Analysis is SEPARATE from booking: analyze_ledger produces findings ABOUT
    the books, never alters them. An optional LLM only rephrases findings.
  - Nothing here holds keys, custodies funds, or signs transactions. Read-only.
"""
from __future__ import annotations

__version__ = "1.0.0"
CAAP1_VERSION = "1.0.0-draft"
__all__ = [
    "books_for_wallet", "sweep_chain", "analyze_ledger", "receipt_to_journal",
    "identify_seller", "resolve_payto", "profile_wallet", "detect_chain",
    "load_config", "CAAP1_VERSION", "__version__",
]


def load_config(path="config.yaml"):
    """Load Counterra config (chains, facilitators, registry, chart of accounts)."""
    import yaml
    return yaml.safe_load(open(path))


def detect_chain(wallet):
    """Infer chain from a wallet address. Returns 'base', 'solana', or None."""
    from counterralib.books import detect_chain as _dc
    return _dc(wallet)


def sweep_chain(wallet, cfg=None, limit=500):
    """
    Fetch an agent wallet's x402 spend from its chain (auto-detected).
    Returns (events, chain, warnings). Read-only, non-custodial.
    """
    from counterralib.books import build_books
    cfg = cfg or load_config()
    return build_books(wallet, cfg, limit=limit)


def books_for_wallet(wallet, cfg=None, limit=500, receipts_dir=None):
    """
    Produce a wallet's books. Returns a dict:
      {chain, summary, journal_entries, exceptions, attribution, events, warnings, period}
    Optionally enriches with x402 receipts from receipts_dir. Writes no files;
    journal entries are CAAP-1 conformant.
    """
    from counterralib.books import build_books, load_receipts, enrich_events_with_receipts
    from counterralib.ledger import (enrich, summarize, journal_entries,
                                     grouped_exceptions, attribution_summary,
                                     latest_full_period)
    cfg = cfg or load_config()
    events, chain, warnings = build_books(wallet, cfg, limit=limit)
    if chain is None:
        return {"chain": None, "warnings": warnings, "events": [], "period": None,
                "journal_entries": [], "exceptions": [], "summary": {}, "attribution": {}}
    if receipts_dir:
        rc = load_receipts(receipts_dir)
        events, _m = enrich_events_with_receipts(events, rc)
    provider_map = _provider_map(cfg)
    rows = enrich(events, cfg.get("agents") or {}, provider_map)
    period = latest_full_period(rows) if rows else None
    return {
        "chain": chain, "warnings": warnings, "events": events, "period": period,
        "summary": summarize(rows),
        "journal_entries": journal_entries(rows, period, cfg.get("accounting")) if period else [],
        "exceptions": grouped_exceptions(rows, cfg.get("accounting")),
        "attribution": attribution_summary(rows),
    }


def analyze_ledger(cfg=None, chain=None, use_llm=False):
    """
    Explain the accumulated ledger. Returns {'findings': [...], 'narration': str}.
    Findings are deterministic; narration only rephrases them. Never alters books.
    """
    from counterralib.analyze import findings, narrate
    cfg = cfg or load_config()
    finds = findings(registry=(cfg.get("providers") or {}), chain=chain)
    return {"findings": finds, "narration": narrate(finds, use_llm=use_llm)}


def receipt_to_journal(receipt, cfg=None):
    """CAAP-1: turn one x402-receipts/v0.3 receipt into an enriched journal entry."""
    from counterralib.receipts import receipt_to_journal as _r2j
    cfg = cfg or load_config()
    acct = (cfg.get("accounting") or {}).get("expense_accounts") or {}
    return _r2j(receipt, registry=_provider_map(cfg), expense_accounts=acct)


def identify_seller(address, cfg=None):
    """Identify a payTo wallet (catalog + builder-code + demand evidence)."""
    from counterralib.whois import identify
    return identify(address)


def resolve_payto(url):
    """Resolve a seller's payTo from its URL. Returns dict or None."""
    from counterralib.resolve import resolve_payto as _rp
    return _rp(url)


def profile_wallet(wallet, cfg=None, db_path=None):
    """Fingerprint an unknown seller from its settlement pattern (leads, not IDs)."""
    from counterralib.profile import profile_wallet as _pw
    cfg = cfg or load_config()
    return _pw(wallet, cfg.get("providers") or {}, db_path=db_path)


def _provider_map(cfg):
    """Merge registry + config providers into {wallet: {label, category}}."""
    import json, os
    merged = {}
    reg_path = os.path.join("docs", "providers.json")
    if os.path.exists(reg_path):
        try:
            reg = json.load(open(reg_path))
            for p in reg.get("providers", []):
                merged[p["wallet"]] = {"label": p["label"], "category": p["category"]}
        except Exception:
            pass
    merged.update(cfg.get("providers") or {})
    return merged
