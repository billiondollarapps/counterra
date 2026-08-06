"""
On-chain name enrichment - identify sellers who never listed in any catalog
but DID leave a name on-chain.

Three signals, all from Blockscout's free public API (no key needed):

  1. Basename / ENS reverse record (`ens_domain_name`) - registered by the
     wallet owner themselves. Mechanical, owner-declared -> VERIFIED tier.
  2. Verified-contract name - compiled into source the deployer verified on
     the explorer. Mechanical -> VERIFIED tier.
  3. Blockscout public tags - community/curator submitted, useful but not
     owner-declared -> PROBABLE tier (pending queue, never auto-published).

This catches the seller who deployed `payments.someservice.base.eth` or a
verified `SomeServicePaymaster` contract without ever touching a Bazaar
catalog - a slice of TomSmart's "85% absent from public discovery" that is
still mechanically nameable.
"""

from __future__ import annotations

from typing import Optional

BLOCKSCOUT_ADDR = "https://base.blockscout.com/api/v2/addresses/{}"


def enrich_wallet(address: str, session=None) -> Optional[dict]:
    """
    Fetch on-chain metadata for a Base wallet.

    Returns {basename, contract_name, tags, is_contract} (any field may be
    None/empty), or None if the lookup failed entirely. Solana addresses
    return None (no Blockscout equivalent wired yet).
    """
    if not str(address).startswith("0x"):
        return None
    if session is None:
        import requests
        session = requests.Session()
    try:
        r = session.get(BLOCKSCOUT_ADDR.format(address), timeout=30)
        r.raise_for_status()
        info = r.json()
    except Exception:
        return None
    tags = [t.get("display_name") or t.get("name")
            for t in (info.get("public_tags") or []) if t]
    tags = [t for t in tags if t]
    return {
        "basename": info.get("ens_domain_name") or None,
        "contract_name": info.get("name") if info.get("is_contract") else None,
        "tags": tags,
        "is_contract": bool(info.get("is_contract")),
    }


def enrichment_candidate(address: str, info: dict) -> Optional[dict]:
    """
    Turn enrichment metadata into a candidate identification.

    Returns {label, category, evidence, tier} or None if nothing nameable.
    tier is "verified" (mechanical, owner-declared) or "probable" (queue it).
    """
    from counterralib.whois import suggest_category

    if not info:
        return None

    if info.get("basename"):
        name = info["basename"]
        return {
            "label": name,
            "category": suggest_category([name]),
            "evidence": ("Basename reverse record: wallet resolves to "
                         "{} (owner-registered on-chain name)".format(name)),
            "tier": "verified",
        }

    if info.get("contract_name"):
        name = info["contract_name"]
        return {
            "label": name,
            "category": suggest_category([name]),
            "evidence": ("Verified contract on Base Blockscout named "
                         "'{}' (deployer-verified source)".format(name)),
            "tier": "verified",
        }

    if info.get("tags"):
        name = info["tags"][0]
        return {
            "label": name,
            "category": suggest_category(info["tags"]),
            "evidence": ("Blockscout public tag(s): {} "
                         "(community-submitted - review before "
                         "publishing)".format(", ".join(info["tags"]))),
            "tier": "probable",
        }

    return None
