"""
Resolve an x402 seller's payTo address from its URL.

Adding a seller to the registry currently means hunting the payTo out of the
seller's discovery document or its HTTP 402 response by hand. This module
automates that lookup so `whois --url <seller>` can go straight from a URL to
an identified, registry-ready entry.

Resolution order, cheapest first:
  1. well-known / catalog docs — GET a set of conventional discovery paths
     (/.well-known/x402, /x402.json, /x402/catalog, ai-catalog, agent-card)
     and pull payTo / ownershipProofs / a CDP Bazaar merchant link out of the
     returned JSON or text.
  2. live 402 challenge — if no doc exposes it, GET the resource itself and
     read the payTo from the x402 PAYMENT-REQUIRED response (body or header).
     POST-only routes are attempted with an empty JSON body as a fallback.

Everything is best-effort and read-only: no payment is ever signed or sent.
Returns the first payTo found with the source it came from, so a human can
sanity-check provenance before trusting it.
"""

from __future__ import annotations

import json
import re
from typing import Optional
from urllib.parse import urljoin, urlparse

EVM_RE = re.compile(r"0x[a-fA-F0-9]{40}")

WELL_KNOWN_PATHS = [
    "/.well-known/x402",
    "/.well-known/x402.json",
    "/x402.json",
    "/x402/catalog",
    "/.well-known/ai-catalog.json",
    "/.well-known/agent-card.json",
    "/.well-known/agent-market.json",
    "/llms.txt",
]

# JSON keys that may carry the payout address, in priority order.
PAYTO_KEYS = ["payTo", "pay_to", "payto", "recipient", "payeeAddress", "address"]
PROOF_KEYS = ["ownershipProofs", "ownership_proofs", "proofs"]


def _origin(url: str) -> str:
    p = urlparse(url if "://" in url else "https://" + url)
    return f"{p.scheme or 'https'}://{p.netloc}"


def _payto_from_obj(obj) -> Optional[str]:
    """Walk a decoded JSON structure for a payTo-like address."""
    found = []

    def walk(o):
        if isinstance(o, dict):
            for k, v in o.items():
                kl = k.lower()
                if kl in [x.lower() for x in PAYTO_KEYS] and isinstance(v, str):
                    m = EVM_RE.search(v)
                    if m:
                        found.append(("key:" + k, m.group(0)))
                if kl in [x.lower() for x in PROOF_KEYS]:
                    for item in (v if isinstance(v, list) else [v]):
                        if isinstance(item, str):
                            m = EVM_RE.search(item)
                            if m:
                                found.append(("ownershipProofs", m.group(0)))
                walk(v)
        elif isinstance(o, list):
            for item in o:
                walk(item)

    walk(obj)
    # A CDP Bazaar merchant link carries ?payTo=0x... — very reliable.
    return found[0] if found else None


def _payto_from_text(text: str):
    """Pull a payTo from a Bazaar merchant URL or a bare address in text."""
    m = re.search(r"payTo=(" + EVM_RE.pattern + r")", text)
    if m:
        return ("bazaar-merchant-link", m.group(1))
    m = EVM_RE.search(text)
    if m:
        return ("address-in-text", m.group(0))
    return None


def _try_doc(session, url):
    try:
        r = session.get(url, timeout=20)
        if r.status_code != 200:
            return None
        # Prefer structured JSON (works even if .text is empty but .json() is set).
        try:
            obj = r.json()
            hit = _payto_from_obj(obj)
            if hit:
                return (url, hit[0], hit[1])
        except ValueError:
            pass
        # Fall back to a raw text scan when there is body text.
        if r.text and r.text.strip():
            hit = _payto_from_text(r.text)
            if hit:
                return (url, hit[0], hit[1])
    except Exception:
        return None
    return None


def _try_402(session, url):
    """Read payTo from a live 402 challenge (header or body)."""
    for method in ("get", "post"):
        try:
            kwargs = {"timeout": 20}
            if method == "post":
                kwargs["json"] = {}
            r = getattr(session, method)(url, **kwargs)
        except Exception:
            continue
        if r.status_code != 402:
            # some servers answer 200/4xx; still scan headers just in case
            pass
        # header form
        for h in ("PAYMENT-REQUIRED", "Payment-Required", "www-authenticate",
                  "x-payment-info"):
            val = r.headers.get(h)
            if val:
                hit = _payto_from_text(val)
                if hit:
                    return (url + f" [{method.upper()} 402 header {h}]", hit[0], hit[1])
        # body form
        try:
            obj = r.json()
            hit = _payto_from_obj(obj)
            if hit:
                return (url + f" [{method.upper()} 402 body]", hit[0], hit[1])
        except Exception:
            hit = _payto_from_text(r.text or "")
            if hit:
                return (url + f" [{method.upper()} 402 body]", hit[0], hit[1])
    return None


def resolve_payto(url: str, session=None):
    """
    Resolve a payTo address from a seller URL.

    Returns {'payto', 'source', 'detail'} on success, or None if no address
    could be found. `source` is a short provenance tag; `detail` is the exact
    location (URL + where in it) so the mapping stays reproducible.
    """
    if session is None:
        import requests
        session = requests.Session()

    origin = _origin(url)

    # 1. conventional discovery docs at the origin
    for path in WELL_KNOWN_PATHS:
        hit = _try_doc(session, urljoin(origin + "/", path.lstrip("/")))
        if hit:
            where, src, addr = hit
            return {"payto": addr, "source": src, "detail": where}

    # 2. the exact URL given, if it is itself a doc
    if url.rstrip("/") not in (origin, origin + "/"):
        hit = _try_doc(session, url)
        if hit:
            where, src, addr = hit
            return {"payto": addr, "source": src, "detail": where}

    # 3. live 402 challenge on the given resource URL
    hit = _try_402(session, url)
    if hit:
        where, src, addr = hit
        return {"payto": addr, "source": src, "detail": where}

    return None
