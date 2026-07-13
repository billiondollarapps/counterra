"""
Seller identification for Scribe.

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
    "https://x402.org/facilitator/discovery/resources",
]


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
        if got_any:
            return


def _extract_paytos(item):
    """Pull every payTo address out of a discovery item, any shape."""
    out = []
    accepts = item.get("accepts") or []
    if isinstance(accepts, dict):
        accepts = [accepts]
    for a in accepts:
        pt = (a or {}).get("payTo") or (a or {}).get("pay_to")
        if pt:
            out.append(str(pt).lower())
    return out


def whois(address, config_providers=None, session=None):
    s = session or requests.Session()
    addr = address.lower()
    print(f"Dossier for {address}")

    # ---- 1. Bazaar / discovery catalogs ----
    matches = []
    print("  searching facilitator discovery catalogs...")
    for item in _bazaar_items(s):
        if addr in _extract_paytos(item):
            res = item.get("resource") or {}
            if isinstance(res, str):
                url, desc = res, ""
            else:
                url = res.get("url", "?")
                desc = res.get("description", "")
            matches.append((url, desc))
    if matches:
        print(f"  BAZAAR MATCH - this wallet sells {len(matches)} resource(s):")
        for url, desc in matches[:10]:
            print(f"    {url}")
            if desc:
                print(f"      \"{desc}\"")
    else:
        print("  no Bazaar listing found (seller may use a private or "
              "non-discoverable endpoint)")

    # ---- 2. Blockscout metadata ----
    try:
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
    except Exception as e:
        print(f"  (Blockscout lookup failed: {e})")

    # ---- 3. suggested config snippet ----
    label = matches[0][0].split("//")[-1].split("/")[0] if matches else "UNKNOWN"
    print("\n  To label this seller, add under providers: in config.yaml:")
    print(f'    "{address}": {{ label: "{label}", category: "AI inference" }}')
    print("  (adjust label/category as appropriate, then re-run the sweep)")
