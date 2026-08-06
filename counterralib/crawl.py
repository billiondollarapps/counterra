"""
x402 endpoint crawler - the registry's highest-yield automatic source.

Mechanics of x402 make sellers self-identifying: a seller's endpoint MUST
disclose its payTo address (in a discovery doc or the 402 challenge itself)
or it cannot get paid. So every publicly-reachable x402 endpoint is
voluntarily publishing its own wallet->service mapping. This module goes and
reads them:

  1. HARVEST candidate URLs
       - GitHub repo search for x402 projects, then scan their READMEs for
         endpoint URLs (uses GITHUB_TOKEN from the environment when present,
         e.g. inside GitHub Actions; works unauthenticated at low rate too).
       - curated list URLs in docs/crawl-seeds.json (awesome-lists, agent
         directories - append as discovered).
       - direct seed URLs in the same file.
  2. RESOLVE each candidate through counterralib.resolve.resolve_payto
     (well-known docs first, then a live 402 challenge - read-only, no
     payment is ever signed).
  3. Every hit is VERIFIED-tier evidence ("this URL's own 402/discovery doc
     names this wallet") and safe to auto-append to the registry.

A small state file (out/crawl_state.json) remembers which hosts were tried
recently so the 6-hourly worker doesn't hammer the same endpoints forever.
"""

from __future__ import annotations

import datetime
import json
import os
import re
from typing import List, Optional, Set

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SEEDS_PATH = os.path.join(HERE, "docs", "crawl-seeds.json")
STATE_PATH = os.path.join(HERE, "out", "crawl_state.json")

GITHUB_SEARCH = "https://api.github.com/search/repositories"
RAW_README = "https://raw.githubusercontent.com/{}/HEAD/README.md"

URL_RE = re.compile(r"https?://[^\s\)\]\"'<>`,]+")

# Well-known token/infrastructure contracts that can NEVER be a seller's
# payTo. Sites mention these in text constantly (e.g. "we settle in USDC:
# 0x8335...") and the text-scan fallback would otherwise scoop them up.
TOKEN_CONTRACTS = {
    "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913",   # USDC (Base)
    "0x036cbd53842c5426634e7929541ec2318f3dcf7e",   # USDC (Base Sepolia)
    "0x4200000000000000000000000000000000000006",   # WETH (Base/OP-stack)
    "0x60a3e35cc302bfa44cb288bc5a4f316fdb1adb42",   # EURC (Base)
    "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48",   # USDC (Ethereum)
    "0xdac17f958d2ee523a2206206994597c13d831ec7",   # USDT (Ethereum)
    "0x0000000000000000000000000000000000000000",   # null address
}

# Sources from resolve_payto that are MECHANICAL (the endpoint structurally
# declares its payTo). Anything else - e.g. a bare address found in page
# text - is circumstantial and must go to the pending queue, never
# auto-publish.
MECHANICAL_SOURCES = ("key:", "ownershipProofs", "bazaar-merchant-link")

# Hosts that are never x402 seller endpoints - don't waste lookups on them.
SKIP_HOSTS = (
    "github.com", "raw.githubusercontent.com", "gist.github.com",
    "twitter.com", "x.com", "t.me", "discord.gg", "discord.com",
    "youtube.com", "youtu.be", "medium.com", "npmjs.com", "www.npmjs.com",
    "pypi.org", "docs.rs", "crates.io", "linkedin.com", "reddit.com",
    "google.com", "shields.io", "img.shields.io", "badge.fury.io",
    "opensource.org", "choosealicense.com", "vercel.app/api/badge",
    "etherscan.io", "basescan.org", "blockscout.com", "solscan.io",
    "coinbase.com", "docs.cdp.coinbase.com", "x402.org", "counterra.xyz",
    "base.org", "go.dev", "nodejs.org", "python.org", "rust-lang.org",
    "modelcontextprotocol.io", "anthropic.com", "openai.com", "glama.ai",
    "vercel.com", "render.com", "railway.app", "supabase.com",
)


def _host(url: str) -> str:
    try:
        return url.split("//", 1)[1].split("/", 1)[0].lower()
    except Exception:
        return ""


def _keep(url: str) -> bool:
    h = _host(url)
    if not h or "." not in h:
        return False
    bare = h.split(":", 1)[0]
    if (bare in ("localhost", "0.0.0.0") or bare.startswith("127.")
            or bare.startswith("192.168.") or bare.startswith("10.")):
        return False
    for s in SKIP_HOSTS:
        if h == s or h.endswith("." + s):
            return False
    return True


def extract_urls(text: str) -> List[str]:
    """Pull candidate endpoint URLs out of free text, one per host."""
    seen_hosts: Set[str] = set()
    out: List[str] = []
    for m in URL_RE.finditer(text or ""):
        u = m.group(0).rstrip(".,;:!?")
        if not _keep(u):
            continue
        h = _host(u)
        if h in seen_hosts:
            continue
        seen_hosts.add(h)
        out.append(u)
    return out


# ------------------------------------------------------------- harvesting --

def _gh_headers() -> dict:
    h = {"Accept": "application/vnd.github+json",
         "User-Agent": "counterra-crawler"}
    tok = os.environ.get("GITHUB_TOKEN")
    if tok:
        h["Authorization"] = "Bearer " + tok
    return h


def harvest_github(session=None, query: str = "x402",
                   max_repos: int = 20) -> List[str]:
    """Find x402 repos, scan their READMEs for endpoint URLs."""
    if session is None:
        import requests
        session = requests.Session()
    urls: List[str] = []
    try:
        r = session.get(GITHUB_SEARCH,
                        params={"q": query, "sort": "updated",
                                "per_page": max_repos},
                        headers=_gh_headers(), timeout=30)
        r.raise_for_status()
        items = (r.json() or {}).get("items") or []
    except Exception:
        return urls
    for it in items[:max_repos]:
        full = (it or {}).get("full_name")
        if not full:
            continue
        try:
            rr = session.get(RAW_README.format(full), timeout=20)
            if rr.status_code == 200 and rr.text:
                urls.extend(extract_urls(rr.text))
        except Exception:
            continue
    return urls


def harvest_lists(list_urls: List[str], session=None) -> List[str]:
    """Fetch curated list pages (raw markdown/text) and extract URLs."""
    if session is None:
        import requests
        session = requests.Session()
    urls: List[str] = []
    for lu in list_urls or []:
        try:
            r = session.get(lu, timeout=20)
            if r.status_code == 200 and r.text:
                urls.extend(extract_urls(r.text))
        except Exception:
            continue
    return urls


def load_seeds(path: Optional[str] = None) -> dict:
    p = path or SEEDS_PATH
    if not os.path.exists(p):
        return {"version": 1, "github_search": True,
                "list_urls": [], "urls": []}
    with open(p) as f:
        return json.load(f)


# ------------------------------------------------------------ crawl state --

def load_state(path: Optional[str] = None) -> dict:
    p = path or STATE_PATH
    if not os.path.exists(p):
        return {}
    try:
        with open(p) as f:
            return json.load(f)
    except Exception:
        return {}


def save_state(state: dict, path: Optional[str] = None):
    p = path or STATE_PATH
    d = os.path.dirname(p)
    if d and not os.path.isdir(d):
        os.makedirs(d)
    with open(p, "w") as f:
        json.dump(state, f, indent=2)


def _fresh(state: dict, host: str, cooldown_days: int) -> bool:
    last = state.get(host)
    if not last:
        return True
    try:
        then = datetime.date.fromisoformat(last[:10])
    except Exception:
        return True
    return (datetime.date.today() - then).days >= cooldown_days


# ------------------------------------------------------------------ crawl --

def crawl(candidate_urls: List[str], known: Set[str], session=None,
          max_lookups: int = 25, cooldown_days: int = 7,
          state: Optional[dict] = None, verbose: bool = True):
    """
    Resolve candidate URLs to registry candidates.

    Returns (verified, probable):
      verified - mechanical evidence (structured payTo key, Bazaar merchant
                 link, or a live 402 challenge). Safe to auto-append.
      probable - a bare address found in page text: circumstantial. Must go
                 to the pending queue for human review, never auto-publish.
    Known token contracts (USDC etc.) are rejected outright - sites mention
    them in prose and they can never be a seller's payTo.
    """
    from counterralib.registry import make_entry
    from counterralib.resolve import resolve_payto
    from counterralib.whois import suggest_category

    if session is None:
        import requests
        session = requests.Session()
    if state is None:
        state = load_state()

    verified: List[dict] = []
    probable: List[dict] = []
    tried = 0
    for url in candidate_urls:
        if tried >= max_lookups:
            break
        h = _host(url)
        if not h or not _fresh(state, h, cooldown_days):
            continue
        tried += 1
        state[h] = datetime.date.today().isoformat()
        try:
            hit = resolve_payto(url, session=session)
        except Exception:
            hit = None
        if not hit:
            if verbose:
                print("  no payTo    {}".format(h))
            continue
        w = hit["payto"]
        wn = w.lower() if w.startswith("0x") else w
        if wn in TOKEN_CONTRACTS:
            if verbose:
                print("  token-contract (rejected)  {} -> {}".format(
                    h, wn[:14] + "..."))
            continue
        if wn in known:
            if verbose:
                print("  known       {} -> {}".format(h, wn[:14] + "..."))
            continue
        src = str(hit.get("source") or "")
        detail = str(hit.get("detail") or "")
        mechanical = (src.startswith(MECHANICAL_SOURCES)
                      or " 402 " in detail)
        chain = "base" if w.startswith("0x") else "solana"
        entry = make_entry(
            wallet=w, chain=chain, label=h,
            category=suggest_category([url]) or "Uncategorized",
            evidence=("payTo resolved from seller's own endpoint: {} "
                      "[{}]".format(detail, src)),
        )
        if mechanical:
            verified.append(entry)
            known.add(wn)
            if verbose:
                print("  RESOLVED    {} -> {}".format(h, wn[:14] + "..."))
        else:
            probable.append(entry)
            if verbose:
                print("  probable    {} -> {}  (address-in-text - "
                      "queued for review)".format(h, wn[:14] + "..."))
    return verified, probable
