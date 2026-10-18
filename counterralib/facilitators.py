"""
Facilitator census - every x402 facilitator wallet, not just six of Coinbase's.

Why this exists: x402 settlements are submitted on-chain BY facilitator
wallets, so the set of facilitator wallets we watch IS the edge of what
Counterra can see. Until now that set was six hand-copied Coinbase wallets on
Base plus six on Solana (three of which the registry has since marked
deprecated). The community registry (Merit-Systems/x402scan) tracks 30+
facilitators - Coinbase, PayAI, Thirdweb, Heurist, Questflow, FluxA, Dexter,
... - with ~90 live settlement wallets across Base and Solana.

This module:
  1. pulls that registry (raw GitHub files - no API key, no auth),
  2. parses every facilitator's wallets per network, with live/deprecated
     status and first-transaction date,
  3. writes docs/facilitators.json (the census - itself a public label set:
     every wallet in it is a VERIFIED facilitator, with the registry as
     evidence),
  4. schedules which wallets a rate-limited sweep should visit next, so a
     free-tier API budget is spent where settlements actually are.

It also names the canonical x402 protocol contracts (Permit2 proxies,
batch-settlement escrow, Multicall3). Facilitators increasingly settle
THROUGH these contracts rather than calling USDC directly, and a payment that
moves through an escrow contract must be booked differently from a direct
payment (a deposit into escrow is a prepaid asset, not an expense).

Python 3.8-safe.
"""

from __future__ import annotations

import datetime
import json
import os
import re
from typing import Dict, List, Optional, Tuple

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CENSUS_PATH = os.path.join(HERE, "docs", "facilitators.json")
SCHEDULE_PATH = os.path.join(HERE, "out", "sweep_schedule.json")

REGISTRY_BASE = ("https://raw.githubusercontent.com/Merit-Systems/x402scan/main/"
                 "packages/external/facilitators/src/facilitators/")
REGISTRY_INDEX = REGISTRY_BASE + "index.ts"

SUPPORTED_CHAINS = ("base", "solana")

# ---------------------------------------------------------------------------
# Canonical x402 protocol contracts on Base (same CREATE2 address on every EVM
# chain). Source: coinbase/x402 contracts/evm/README.md "Canonical Addresses".
# kind drives booking:
#   permit2-proxy   - payer -> payee transfer routed through Permit2; a direct
#                     payment, books exactly like an EIP-3009 settlement
#   batch-escrow    - x402BatchSettlement: payer DEPOSITS into a channel
#                     (prepaid escrow, not expense); later SETTLES to the
#                     seller or REFUNDS the payer
#   batch-collector - deposit collectors that pull into the escrow
#   multicall       - Multicall3, used by facilitators to batch calls
#   permit2         - Uniswap Permit2 itself
X402_CONTRACTS = {
    "0x402085c248eea27d92e8b30b2c58ed07f9e20001": ("x402ExactPermit2Proxy", "permit2-proxy"),
    "0x4020a4f3b7b90cca423b9fabcc0ce57c6c240002": ("x402UptoPermit2Proxy", "permit2-proxy"),
    "0x4020074e9df2ce1dee5a9c1b5c3f541d02a10003": ("x402BatchSettlement", "batch-escrow"),
    "0x4020806089470a89826cb9fb1f4059150b550004": ("ERC3009DepositCollector", "batch-collector"),
    "0x4020425faf3b746c082c2f942b4e5159887b0005": ("Permit2DepositCollector", "batch-collector"),
    "0xca11bde05977b3631167028862be2a173976ca11": ("Multicall3", "multicall"),
    "0x000000000022d473030f116ddee9f6b43ac78ba3": ("Permit2", "permit2"),
}
BATCH_ESCROW = {a for a, (_, k) in X402_CONTRACTS.items() if k == "batch-escrow"}
BATCH_COLLECTORS = {a for a, (_, k) in X402_CONTRACTS.items() if k == "batch-collector"}


def contract_info(addr: str) -> Optional[Tuple[str, str]]:
    return X402_CONTRACTS.get(str(addr).lower())


def _norm(addr: str) -> str:
    a = str(addr)
    return a.lower() if a.startswith("0x") else a


# ------------------------------------------------------------------ parsing --

def parse_index(src: str) -> List[str]:
    """Module names listed in the registry's facilitators/index.ts."""
    mods = re.findall(r"from\s+'\./([\w-]+)'", src)
    seen, out = set(), []
    for m in mods:
        if m not in seen:
            seen.add(m)
            out.append(m)
    return out


def parse_facilitator_ts(src: str, module: str = "") -> List[dict]:
    """
    Parse one facilitator file into wallet entries:
        {facilitator, name, chain, address, deprecated, first_tx}

    The files are TypeScript object literals; we parse the stable shape
    (`[Network.X]: [ { address: '...', deprecated?: true,
    dateOfFirstTransaction: new Date('YYYY-MM-DD') }, ... ]`) rather than
    executing anything.
    """
    fid = re.search(r"\bid:\s*'([^']+)'", src)
    name = re.search(r"\bname:\s*'([^']+)'", src)
    fac_id = fid.group(1) if fid else module
    fac_name = name.group(1) if name else module
    # A whole facilitator can be deprecated at the top level of its object.
    top_deprecated = bool(re.search(r"^\s{2}deprecated:\s*true", src, re.M))
    out = []
    for m in re.finditer(r"\[Network\.(\w+)\]:\s*\[(.*?)\n\s*\],?", src, re.S):
        chain = m.group(1).lower()
        for e in re.finditer(r"\{(.*?)\}", m.group(2), re.S):
            body = e.group(1)
            a = re.search(r"address:\s*'([^']+)'", body)
            if not a:
                continue
            d = re.search(r"Date\('([\d-]+)'\)", body)
            out.append({
                "facilitator": fac_id,
                "name": fac_name,
                "chain": chain,
                "address": _norm(a.group(1)),
                "deprecated": bool(re.search(r"deprecated:\s*true", body)) or top_deprecated,
                "first_tx": d.group(1) if d else None,
            })
    return out


# ----------------------------------------------------------------- fetching --

def fetch_census(session=None, verbose: bool = False) -> dict:
    """Pull the whole x402scan facilitator registry into one census dict."""
    if session is None:
        import requests
        session = requests.Session()
    idx = session.get(REGISTRY_INDEX, timeout=30)
    if getattr(idx, "status_code", 200) != 200:
        raise RuntimeError("registry index HTTP {}".format(idx.status_code))
    modules = parse_index(idx.text)
    if not modules:
        raise RuntimeError("registry index format changed - no modules found; "
                           "update parse_index in counterralib/facilitators.py")
    wallets, failed = [], []
    for mod in modules:
        try:
            r = session.get(REGISTRY_BASE + mod + ".ts", timeout=30)
            if getattr(r, "status_code", 200) != 200:
                failed.append(mod)
                continue
            got = parse_facilitator_ts(r.text, mod)
            wallets.extend(got)
            if verbose:
                print("  {:<18} {} wallet(s)".format(mod, len(got)))
        except Exception:
            failed.append(mod)
    return make_census(wallets, failed=failed)


def make_census(wallets: List[dict], failed: Optional[List[str]] = None) -> dict:
    # de-duplicate (same wallet listed twice -> keep the live one)
    by_key: Dict[Tuple[str, str], dict] = {}
    for w in wallets:
        k = (w["chain"], w["address"])
        prev = by_key.get(k)
        if prev is None or (prev["deprecated"] and not w["deprecated"]):
            by_key[k] = w
    rows = sorted(by_key.values(),
                  key=lambda w: (w["chain"], w["deprecated"], w["name"].lower(),
                                 w["first_tx"] or ""))
    return {
        "$schema_note": ("Counterra facilitator census: every x402 facilitator "
                         "settlement wallet in the x402scan community registry. "
                         "Each entry is a VERIFIED facilitator label (evidence: "
                         "the registry). Regenerate with: counterra.py census --write"),
        "version": 1,
        "generated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
        "source": REGISTRY_BASE,
        "failed_modules": failed or [],
        "summary": summarize(rows),
        "wallets": rows,
    }


def summarize(rows: List[dict]) -> dict:
    out: Dict[str, dict] = {}
    for w in rows:
        c = out.setdefault(w["chain"], {"live": 0, "deprecated": 0, "facilitators": set()})
        c["deprecated" if w["deprecated"] else "live"] += 1
        if not w["deprecated"]:
            c["facilitators"].add(w["name"])
    for c in out.values():
        c["facilitators"] = sorted(c["facilitators"])
    return out


def load_census(path: Optional[str] = None) -> Optional[dict]:
    p = path or CENSUS_PATH
    if not os.path.exists(p):
        return None
    try:
        with open(p) as f:
            return json.load(f)
    except Exception:
        return None


def save_census(census: dict, path: Optional[str] = None):
    p = path or CENSUS_PATH
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w") as f:
        json.dump(census, f, indent=1)
        f.write("\n")


def live_wallets(census: Optional[dict], chain: str) -> List[dict]:
    if not census:
        return []
    return [w for w in census.get("wallets", [])
            if w["chain"] == chain and not w.get("deprecated")]


def facilitator_index(census: Optional[dict]) -> Dict[str, dict]:
    """{address: entry} for every census wallet, live AND deprecated - a
    deprecated facilitator wallet is still a facilitator in historical data."""
    if not census:
        return {}
    return {w["address"]: w for w in census.get("wallets", [])}


# --------------------------------------------------------------- scheduling --
#
# The free public APIs allow a few dozen calls per run. 70+ Base wallets x
# several calls each will not fit. So each run visits the k wallets with the
# best expected yield:  score = observed settlements/hour x hours since last
# visit  (a wallet we have never visited scores highest, so every wallet is
# probed at least once). Busy wallets get revisited often; idle ones rarely.

def _hours_since(iso: Optional[str], now: datetime.datetime) -> float:
    if not iso:
        return 1e6
    try:
        t = datetime.datetime.fromisoformat(iso)
        if t.tzinfo is None:
            t = t.replace(tzinfo=datetime.timezone.utc)
        return max(0.0, (now - t).total_seconds() / 3600.0)
    except Exception:
        return 1e6


def load_schedule(path: Optional[str] = None) -> dict:
    p = path or SCHEDULE_PATH
    try:
        with open(p) as f:
            return json.load(f)
    except Exception:
        return {}


def save_schedule(state: dict, path: Optional[str] = None):
    p = path or SCHEDULE_PATH
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w") as f:
        json.dump(state, f, indent=1, sort_keys=True)
        f.write("\n")


def pick_wallets(addresses: List[str], state: dict, k: int,
                 now: Optional[datetime.datetime] = None) -> List[str]:
    now = now or datetime.datetime.now(datetime.timezone.utc)
    scored = []
    for i, a in enumerate(addresses):
        s = state.get(a) or {}
        hrs = _hours_since(s.get("last_visit"), now)
        if hrs >= 1e6:
            score = 1e12 - i            # never visited: first, in list order
        else:
            rate = float(s.get("rate_per_hour", 0.0))
            # floor keeps quiet wallets on a slow rotation instead of never
            score = max(rate, 0.05) * hrs
        scored.append((score, a))
    scored.sort(reverse=True)
    return [a for _, a in scored[:max(0, k)]]


def record_visit(state: dict, address: str, listed: int, newest_ts: Optional[str],
                 oldest_ts: Optional[str], now: Optional[datetime.datetime] = None):
    """Update a wallet's observed settlement rate after a visit.

    rate = settlements listed / span they cover (hours). An empty listing
    decays the rate so a wallet that went quiet drifts down the queue.
    """
    now = now or datetime.datetime.now(datetime.timezone.utc)
    s = state.setdefault(address, {})
    rate = 0.0
    if listed and newest_ts and oldest_ts:
        span = max(_hours_since(oldest_ts, now) - _hours_since(newest_ts, now), 0.25)
        rate = listed / span
        # A full page means we saw only the newest slice: the true rate is
        # at least this high, never lower.
    prev = float(s.get("rate_per_hour", rate))
    s["rate_per_hour"] = round(0.5 * prev + 0.5 * rate, 4) if s.get("last_visit") else round(rate, 4)
    s["last_visit"] = now.isoformat(timespec="seconds")
    s["last_listed"] = int(listed)
    if newest_ts:
        s["newest_seen"] = newest_ts


def record_error(state: dict, address: str, error: str = "",
                 now: Optional[datetime.datetime] = None):
    """A failed visit still counts as a visit (rate unchanged), so one wallet
    that keeps erroring cannot pin itself to the front of the queue."""
    now = now or datetime.datetime.now(datetime.timezone.utc)
    s = state.setdefault(address, {})
    s["last_visit"] = now.isoformat(timespec="seconds")
    s.setdefault("rate_per_hour", 0.0)
    s["errors"] = int(s.get("errors", 0)) + 1
    s["last_error"] = str(error)[:160]
