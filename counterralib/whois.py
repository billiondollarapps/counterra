"""
Seller identification for Counterra.

`whois(address)` builds a dossier on a payee wallet:
  1. Bazaar match - x402 sellers advertise endpoints with their payTo
     address in facilitator discovery catalogs. A match reveals the
     seller's URL, description, and price.
  2. Blockscout metadata - name tags, contract status.

Every identified seller can then be labeled in config.yaml, turning
"Uncategorized Agent Spend" into a named expense line.
"""
import requests

DISCOVERY_ENDPOINTS = [
    "https://api.cdp.coinbase.com/platform/v2/x402/discovery/resources",
    "https://facilitator.payai.network/discovery/resources",
]


def _norm(addr):
    """EVM addresses compare case-insensitively; Solana base58 is
    case-SENSITIVE and must match exactly."""
    a = str(addr)
    return a.lower() if a.startswith("0x") else a


def _bazaar_items(session, limit_pages=12):
    """Yield resource items from the first discovery endpoint that works."""
    for base in DISCOVERY_ENDPOINTS:
        offset, got_any = 0, False
        while True:
            try:
                r = session.get(base, params={"limit": 100, "offset": offset},
                                timeout=30)
                r.raise_for_status()
                data = r.json()
            except Exception as e:
                if not got_any:
                    print(f"  (discovery endpoint unavailable: {base} -> {e})")
                break
            items = data.get("items") or data.get("resources") or []
            if not items:
                break
            got_any = True
            for it in items:
                yield it
            pag = data.get("pagination") or {}
            total = pag.get("total", 0)
            offset += pag.get("limit", len(items))
            if offset >= total or offset // 100 >= limit_pages:
                break


def _extract_paytos(item):
    """Pull every payTo address out of a discovery item, any shape."""
    out = []
    accepts = item.get("accepts") or []
    if isinstance(accepts, dict):
        accepts = [accepts]
    for a in accepts:
        pt = (a or {}).get("payTo") or (a or {}).get("pay_to")
        if pt:
            out.append(_norm(pt))
    return out


CATEGORY_HINTS = [
    (("search", "news", "feed", "market", "price", "defillama", "data",
      "scrape", "crawl", "enrich"), "Market data"),
    (("llm", "inference", "generate", "image", "fal", "model", "ai"), "AI inference"),
    (("gpu", "compute", "sandbox", "render", "task"), "Compute"),
    (("parse", "document", "pdf", "ocr"), "Document parsing"),
]


def suggest_category(urls):
    text = " ".join(urls).lower()
    for keys, cat in CATEGORY_HINTS:
        if any(k in text for k in keys):
            return cat
    return "Uncategorized"


def identify(address, session=None):
    """Programmatic identification. Returns:
    {chain, matches:[(url,desc)], label, evidence, category_suggestion}"""
    s = session or requests.Session()
    addr = _norm(address)
    chain = "base" if str(address).startswith("0x") else "solana"
    matches = []
    for item in _bazaar_items(s):
        if addr in _extract_paytos(item):
            res = item.get("resource") or {}
            if isinstance(res, str):
                url, desc = res, ""
            else:
                url = res.get("url", "?")
                desc = res.get("description", "")
            matches.append((url, desc))
    label = matches[0][0].split("//")[-1].split("/")[0] if matches else None
    evidence = (f"Discovery catalog payTo match: {len(matches)} resources at {label}/*"
                if matches else None)
    return {"chain": chain, "matches": matches, "label": label,
            "evidence": evidence,
            "category_suggestion": suggest_category([u for u, _ in matches]) if matches else None}


def whois(address, config_providers=None, session=None):
    print(f"Dossier for {address}")
    print("  searching facilitator discovery catalogs...")
    ident = identify(address, session=session)
    matches = ident["matches"]
    is_evm = str(address).startswith("0x")
    if matches:
        print(f"  BAZAAR MATCH - this wallet sells {len(matches)} resource(s):")
        for url, desc in matches[:10]:
            print(f"    {url}")
            if desc:
                print(f"      \"{desc}\"")
    else:
        print("  no Bazaar listing found (seller may use a private or "
              "non-discoverable endpoint)")

    # ---- 2. Chain metadata ----
    if not is_evm:
        print(f"  Solana address - inspect manually: https://solscan.io/account/{address}")
    try:
        if not is_evm:
            raise StopIteration
        r = s.get(f"https://base.blockscout.com/api/v2/addresses/{address}",
                  timeout=30)
        r.raise_for_status()
        info = r.json()
        name = info.get("name")
        tags = [t.get("display_name") or t.get("name")
                for t in (info.get("public_tags") or [])]
        kind = "contract" if info.get("is_contract") else "wallet (EOA)"
        print(f"  Blockscout: {kind}"
              + (f", name: {name}" if name else "")
              + (f", tags: {', '.join(tags)}" if tags else ""))
    except StopIteration:
        pass
    except Exception as e:
        print(f"  (Blockscout lookup failed: {e})")

    # ---- 3. suggested config snippet ----
    label = ident["label"] or "UNKNOWN"
    chain = ident["chain"]
    ev = ident["evidence"] or "REPLACE WITH EVIDENCE - no catalog match found"
    import json, datetime
    entry = {"wallet": address if chain == "solana" else address.lower(),
             "chain": chain, "label": label,
             "category": ((ident["category_suggestion"] or "REPLACE") + " (auto-suggested - review)") if matches else "REPLACE",
             "evidence": ev, "added": datetime.date.today().isoformat()}
    print("\n  Ready-to-PR registry entry for docs/providers.json:")
    print("  " + json.dumps(entry, indent=2).replace("\n", "\n  "))
    print("  (set the category, verify the evidence, then PR - see CONTRIBUTING.md)")
