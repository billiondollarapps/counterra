"""
Network label book - every wallet labelled, every payment categorised.

The ledger (out/counterra.db) holds every x402 settlement Counterra has swept.
This module turns that raw stream into the asset: a label for EVERY wallet
that appears in it and a category for EVERY payment, each with an honest
evidence tier, plus the one number that says how far along the map is -
coverage, by settlements and by dollars.

Evidence tiers (strongest first). A wallet gets the strongest tier it can
prove; nothing is ever promoted on weaker evidence:

  hard-stop   suspected address-poisoning clone of an established counterparty
              (outranks everything - a payment to it is a loss, not spend)
  verified    mechanical evidence: registry entry (catalog payTo match, live
              402 resolution, Basename...) or the facilitator census
  protocol    a canonical x402 contract (Permit2 proxy, batch escrow,
              Multicall3) - identity is the contract itself
  probable    circumstantial evidence awaiting human review (pending queue)
  behavioral  unnamed, but the settlement pattern says what KIND of spend it
              is (metered API, tiered service, recurring, bulk) - never a name
  unknown     seen, not yet labelled - the identification work queue
  role-only   a buyer (agent). Buyers are never named: the registry is
              sellers-only by design. They get a role and a behaviour class.

Wash/test screening. Public x402 totals are inflated by test traffic and
self-dealing (Visa/Artemis publish "adjusted" volume for this reason). Every
payment is screened and flagged - flagged payments stay in the books but are
excluded from the "adjusted" coverage numbers:

  self-payment   payer == payee
  dust           <= 1 micro-USDC
  round-trip     money flows both ways between the same pair
  closed-loop    REVIEW ONLY, not excluded: a seller whose income is >=95%
                 from ONE buyer that in turn spends >=95% with that seller
                 (>=10 settlements). Could be an operator testing against
                 itself - or simply a vendor with one big customer (the
                 largest real case in the ledger is a verified vendor's main
                 client). Flagged for a human, never silently dropped.

Python 3.8-safe. No network access: this reads the ledger and local files.
"""

from __future__ import annotations

import datetime
import json
import os
from collections import defaultdict
from typing import Dict, Iterable, List, Optional

from counterralib import facilitators as fac_mod
from counterralib import shapes as shape_mod

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LABELS_PATH = os.path.join(HERE, "docs", "labels.json")
COVERAGE_PATH = os.path.join(HERE, "docs", "coverage.json")

TIER_ORDER = ("hard-stop", "verified", "protocol", "probable", "behavioral",
              "unknown", "role-only")
LABELLED_TIERS = ("hard-stop", "verified", "protocol", "probable", "behavioral")
NAMED_TIERS = ("verified", "probable")

DUST_USDC = 0.000001
# Flags that remove a payment from "adjusted" volume. closed-loop is shown
# but deliberately NOT here - see module doc.
EXCLUDE_FLAGS = ("self-payment", "dust", "round-trip")
CLOSED_LOOP_SHARE = 0.95
CLOSED_LOOP_MIN = 10

from counterralib.ledger import (ESCROW_DEPOSIT as CAT_ESCROW_DEPOSIT,  # noqa: E402
                                 ESCROW_PAYOUT as CAT_ESCROW_PAYOUT,
                                 ESCROW_ACCOUNT)
UNCATEGORIZED = "Uncategorized"

# Payment legs that are an agent paying a seller (booked as spend).
PAYMENT_LEGS = ("exact", "permit2", "multicall", "routed", "wallet-tracked")

# Explorer pages mention every address - an address merely appearing on one
# proves nothing about who owns it. Pending entries from these hosts are junk.
from counterralib.crawl import EXPLORER_HOST_RE  # noqa: E402  (one definition)


def norm(addr) -> str:
    a = str(addr or "")
    return a.lower() if a.startswith("0x") else a


def leg_type(memo: str) -> str:
    m = str(memo or "")
    if m.startswith("x402:"):
        return m.split(":", 1)[1] or "exact"
    if m == "tokentx":
        return "wallet-tracked"
    return "exact"      # legacy rows: "facilitator-settled" were direct payments


def _iso(ts) -> str:
    return ts.isoformat() if hasattr(ts, "isoformat") else str(ts)


def _median(xs):
    if not xs:
        return None
    s = sorted(xs)
    m = len(s) // 2
    return s[m] if len(s) % 2 else (s[m - 1] + s[m]) / 2.0


def event_key(e) -> str:
    return f"{e.chain}:{e.tx_hash}:{e.payer_wallet}:{e.payee_wallet}:{e.amount_usdc}"


# --------------------------------------------------------------- inputs ----

def _load_json(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return default


def registry_index(registry: dict) -> Dict[str, dict]:
    return {norm(p["wallet"]): p for p in (registry or {}).get("providers", [])}


def pending_index(pending: dict) -> Dict[str, dict]:
    out = {}
    for p in (pending or {}).get("pending", []):
        if EXPLORER_HOST_RE.search(str(p.get("label", ""))):
            continue
        out[norm(p["wallet"])] = p
    return out


# ------------------------------------------------------------ screening ----

def screen(events: List) -> Dict[str, List[str]]:
    """{event_key: [flags]} for wash/test patterns (see module doc)."""
    flags: Dict[str, List[str]] = defaultdict(list)
    pay = [e for e in events if leg_type(e.memo) in PAYMENT_LEGS]
    seller_in = defaultdict(lambda: defaultdict(float))   # seller -> buyer -> usd
    buyer_out = defaultdict(lambda: defaultdict(float))   # buyer -> seller -> usd
    pair_n = defaultdict(int)
    for e in pay:
        b, s, a = norm(e.payer_wallet), norm(e.payee_wallet), float(e.amount_usdc)
        seller_in[s][b] += a
        buyer_out[b][s] += a
        pair_n[(b, s)] += 1
    loops = set()
    for s, by in seller_in.items():
        tot = sum(by.values())
        if tot <= 0:
            continue
        b, top = max(by.items(), key=lambda kv: kv[1])
        if pair_n[(b, s)] < CLOSED_LOOP_MIN or top / tot < CLOSED_LOOP_SHARE:
            continue
        btot = sum(buyer_out[b].values())
        if btot > 0 and buyer_out[b][s] / btot >= CLOSED_LOOP_SHARE:
            loops.add((b, s))
    for e in pay:
        k = event_key(e)
        b, s = norm(e.payer_wallet), norm(e.payee_wallet)
        if b == s:
            flags[k].append("self-payment")
        if float(e.amount_usdc) <= DUST_USDC:
            flags[k].append("dust")
        if (b, s) in loops:
            flags[k].append("closed-loop")
        if (s, b) in pair_n:
            flags[k].append("round-trip")
    return dict(flags)


def poison_by_buyer(events: List, registry_keys: Iterable[str]) -> Dict[str, dict]:
    """Address-poisoning detection, done the way it actually works: per buyer.

    A clone impersonates a counterparty of ONE operator (copied from that
    operator's own history), so each buyer is checked against its own
    established counterparties plus the registry. Running it network-wide
    would compare every wallet with every other and drown in noise.
    """
    from counterralib import poison
    reg = set(registry_keys)
    by_buyer = defaultdict(list)
    for e in events:
        if leg_type(e.memo) in PAYMENT_LEGS and norm(e.payee_wallet) not in fac_mod.X402_CONTRACTS:
            by_buyer[norm(e.payer_wallet)].append(e)
    found: Dict[str, dict] = {}
    for evs in by_buyer.values():
        if len({norm(e.payee_wallet) for e in evs}) < 2 and not reg:
            continue
        for w, f in poison.detect_poisoning(evs, registry_wallets=reg).items():
            if w in reg:
                continue
            prev = found.get(w)
            if prev is None or (f["confidence"] == "high" and prev["confidence"] != "high"):
                found[w] = f
    return found


# ------------------------------------------------------------- building ----

def buyer_class(n: int, sellers: int, med_gap: Optional[float]) -> str:
    if n <= 1:
        return "one-off payer"
    if n >= 50 and med_gap is not None and med_gap < 120:
        return "high-frequency agent"
    if sellers >= 5:
        return "multi-seller agent"
    if sellers == 1:
        return "single-seller buyer"
    return "agent"


def build(events: List, registry: Optional[dict] = None,
          pending: Optional[dict] = None, census: Optional[dict] = None,
          accounting: Optional[dict] = None) -> dict:
    """Label every wallet and categorise every payment in `events`."""
    reg = registry_index(registry or {})
    pend = pending_index(pending or {})
    facs = {w: f for w, f in fac_mod.facilitator_index(census).items()
            if f["chain"] in fac_mod.SUPPORTED_CHAINS}
    exp_accounts = shape_mod.merged_expense_accounts(
        (accounting or {}).get("expense_accounts") or {})
    from counterralib import poison
    exp_accounts.setdefault(poison.CAT_POISONING, poison.POISONING_ACCOUNT)
    exp_accounts.setdefault(UNCATEGORIZED, "6490 - Uncategorized Agent Spend")

    flags = screen(events)
    poisons = poison_by_buyer(events, reg.keys())

    # ---- aggregate ----
    S = defaultdict(lambda: {"n": 0, "usd": 0.0, "cp": set(), "first": None,
                             "last": None, "evs": [], "chain": ""})
    B = defaultdict(lambda: {"n": 0, "usd": 0.0, "cp": set(), "first": None,
                             "last": None, "ts": [], "chain": ""})
    depositors, escrow_receivers = set(), set()
    for e in events:
        lt = leg_type(e.memo)
        p, q, a, t = norm(e.payer_wallet), norm(e.payee_wallet), float(e.amount_usdc), _iso(e.ts)
        if lt == "batch-deposit":
            depositors.add(p)
        if lt == "batch-payout":
            escrow_receivers.add(q)
        if lt in PAYMENT_LEGS or lt == "batch-payout":
            s = S[q]
            s["n"] += 1; s["usd"] += a; s["cp"].add(p); s["chain"] = e.chain
            s["first"] = min(s["first"] or t, t); s["last"] = max(s["last"] or t, t)
            if lt in PAYMENT_LEGS:
                # shape uses per-call payments only: an escrow payout is a lump
                # sum of many vouchers and would distort the price ladder
                s["evs"].append(e)
        if lt in PAYMENT_LEGS or lt == "batch-deposit":
            b = B[p]
            b["n"] += 1; b["usd"] += a; b["cp"].add(q); b["chain"] = e.chain
            b["first"] = min(b["first"] or t, t); b["last"] = max(b["last"] or t, t)
            b["ts"].append(e.ts)

    unknown_sellers = [w for w in S if w not in reg and w not in facs
                       and w not in fac_mod.X402_CONTRACTS and w not in poisons]
    shapes = {}
    for w in unknown_sellers:
        sh = shape_mod.classify_shape(S[w]["evs"])
        if sh:
            shapes[w] = sh

    wallets: Dict[str, dict] = {}

    def rec(w, chain):
        r = wallets.get(w)
        if r is None:
            r = wallets[w] = {"wallet": w, "chain": chain, "roles": [],
                              "name": "", "category": "", "tier": "unknown",
                              "evidence": "", "confidence": "", "flags": []}
        return r

    # sellers
    for w, s in S.items():
        r = rec(w, s["chain"])
        r["roles"].append("seller")
        if w in escrow_receivers and w in depositors:
            r["roles"].append("channel-refund-recipient")
        elif w in escrow_receivers:
            r["roles"].append("batch-channel-receiver")
        r["as_seller"] = {
            "settlements": s["n"], "usd": round(s["usd"], 6),
            "buyers": len(s["cp"]), "first_seen": s["first"], "last_seen": s["last"],
            "price_points": len({round(float(e.amount_usdc), 6) for e in s["evs"]}),
        }
        if w in poisons:
            f = poisons[w]
            r.update(tier="hard-stop", name=f["label"], category=f["category"],
                     evidence=f["rationale"], confidence=f.get("confidence", ""))
        elif w in reg:
            p = reg[w]
            r.update(tier="verified", name=p.get("label", ""),
                     category=p.get("category") or UNCATEGORIZED,
                     evidence=p.get("evidence", ""))
        elif w in pend:
            p = pend[w]
            r.update(tier="probable", name=p.get("label", ""),
                     category=p.get("category") or UNCATEGORIZED,
                     evidence=p.get("evidence", ""), confidence=p.get("confidence", ""))
        elif w in shapes:
            sh = shapes[w]
            r.update(tier="behavioral", name=sh["label"], category=sh["category"],
                     evidence=sh["rationale"], confidence=sh.get("confidence", ""))
        else:
            r.update(category=UNCATEGORIZED)

    # buyers
    for w, b in B.items():
        r = rec(w, b["chain"])
        ts = sorted(b["ts"])
        gaps = [(ts[i + 1] - ts[i]).total_seconds() for i in range(len(ts) - 1)]
        cls = buyer_class(b["n"], len(b["cp"]), _median(gaps))
        r["roles"].append("buyer")
        if w in depositors:
            r["roles"].append("channel-depositor")
        r["as_buyer"] = {
            "settlements": b["n"], "usd": round(b["usd"], 6), "sellers": len(b["cp"]),
            "first_seen": b["first"], "last_seen": b["last"], "class": cls,
        }
        if "seller" not in r["roles"]:
            r.update(tier="role-only", name="", category=cls,
                     evidence="pays x402 sellers via facilitator settlements")

    # facilitators and protocol contracts override role labels
    # (census wallets are labelled even when the ledger has not seen them:
    # the census is itself part of the public label set)
    for w, f in facs.items():
        r = rec(w, f["chain"])
        if "facilitator" not in r["roles"]:
            r["roles"].append("facilitator")
        if r["tier"] in ("unknown", "role-only", "behavioral"):
            r.update(tier="verified", name=f["name"] + " facilitator",
                     category="x402 facilitator",
                     evidence="x402scan facilitator registry ({}{})".format(
                         f["facilitator"], ", deprecated" if f.get("deprecated") else ""))
    for w, (name, kind) in fac_mod.X402_CONTRACTS.items():
        r = rec(w, "base")
        r["roles"].append("x402-contract")
        r.update(tier="protocol", name=name, category="x402 protocol contract (" + kind + ")",
                 evidence="canonical x402 contract address (coinbase/x402 contracts/evm)")

    # ---- payments ----
    payments = []
    wallet_flags = defaultdict(set)
    for e in events:
        k = event_key(e)
        lt = leg_type(e.memo)
        q = norm(e.payee_wallet)
        seller = wallets.get(q, {})
        fl = flags.get(k, [])
        if lt == "batch-deposit":
            cat, tier, account = CAT_ESCROW_DEPOSIT, "protocol", ESCROW_ACCOUNT
        elif lt == "batch-payout":
            cat, tier, account = CAT_ESCROW_PAYOUT, seller.get("tier", "unknown"), ESCROW_ACCOUNT
        else:
            cat = seller.get("category") or UNCATEGORIZED
            tier = seller.get("tier", "unknown")
            account = exp_accounts.get(cat, exp_accounts[UNCATEGORIZED])
        payments.append({
            "event_key": k, "chain": e.chain, "ts": _iso(e.ts), "tx_hash": e.tx_hash,
            "payer": norm(e.payer_wallet), "payee": q,
            "amount_usdc": float(e.amount_usdc), "leg_type": lt,
            "facilitator": getattr(e, "facilitator", "") or "",
            "seller_name": seller.get("name", ""), "category": cat,
            "tier": tier, "account": account, "flags": fl,
        })
        for f in fl:
            wallet_flags[norm(e.payer_wallet)].add(f)
            wallet_flags[q].add(f)
    for w, fs in wallet_flags.items():
        if w in wallets:
            wallets[w]["flags"] = sorted(fs)

    return {"wallets": wallets, "payments": payments,
            "coverage": coverage(payments, wallets)}


# ------------------------------------------------------------- coverage ----

def excluded(p: dict) -> bool:
    return any(f in EXCLUDE_FLAGS for f in p.get("flags") or [])


def coverage(payments: List[dict], wallets: Dict[str, dict]) -> dict:
    def bucket():
        return {"settlements": 0, "usd": 0.0}

    def summarize(rows):
        by_tier = defaultdict(bucket)
        tot, adj = bucket(), bucket()
        flagged = bucket()
        for p in rows:
            if p["leg_type"] not in PAYMENT_LEGS:
                continue
            b = by_tier[p["tier"]]
            b["settlements"] += 1; b["usd"] += p["amount_usdc"]
            tot["settlements"] += 1; tot["usd"] += p["amount_usdc"]
            if excluded(p):
                flagged["settlements"] += 1; flagged["usd"] += p["amount_usdc"]
            else:
                adj["settlements"] += 1; adj["usd"] += p["amount_usdc"]

        def pct(tiers, key, base):
            v = sum(by_tier[t][key] for t in tiers if t in by_tier)
            return round(100.0 * v / base[key], 1) if base[key] else 0.0

        adj_rows = [p for p in rows if p["leg_type"] in PAYMENT_LEGS and not excluded(p)]
        adj_tier = defaultdict(bucket)
        for p in adj_rows:
            adj_tier[p["tier"]]["settlements"] += 1
            adj_tier[p["tier"]]["usd"] += p["amount_usdc"]

        def adj_pct(tiers, key):
            v = sum(adj_tier[t][key] for t in tiers if t in adj_tier)
            return round(100.0 * v / adj[key], 1) if adj[key] else 0.0

        return {
            "payments": {"settlements": tot["settlements"], "usd": round(tot["usd"], 2)},
            "adjusted": {"settlements": adj["settlements"], "usd": round(adj["usd"], 2)},
            "flagged": {"settlements": flagged["settlements"], "usd": round(flagged["usd"], 2)},
            "labelled_pct": {"settlements": pct(LABELLED_TIERS, "settlements", tot),
                             "usd": pct(LABELLED_TIERS, "usd", tot)},
            "named_pct": {"settlements": pct(NAMED_TIERS, "settlements", tot),
                          "usd": pct(NAMED_TIERS, "usd", tot)},
            "adjusted_labelled_pct": {"settlements": adj_pct(LABELLED_TIERS, "settlements"),
                                      "usd": adj_pct(LABELLED_TIERS, "usd")},
            "adjusted_named_pct": {"settlements": adj_pct(NAMED_TIERS, "settlements"),
                                   "usd": adj_pct(NAMED_TIERS, "usd")},
            "by_tier": {t: {"settlements": v["settlements"], "usd": round(v["usd"], 2)}
                        for t, v in sorted(by_tier.items(),
                                           key=lambda kv: TIER_ORDER.index(kv[0])
                                           if kv[0] in TIER_ORDER else 99)},
        }

    chains = sorted({p["chain"] for p in payments})
    out = {"overall": summarize(payments)}
    for c in chains:
        out[c] = summarize([p for p in payments if p["chain"] == c])

    # wallet counts
    wc = defaultdict(lambda: defaultdict(int))
    for w in wallets.values():
        for role in w["roles"]:
            if role in ("seller", "buyer", "facilitator", "x402-contract"):
                wc[role][w["tier"]] += 1
    out["wallets"] = {r: dict(v) for r, v in wc.items()}

    # identification work queue: unnamed sellers by dollars, flagged ones last
    q = [w for w in wallets.values()
         if "seller" in w["roles"] and w["tier"] in ("unknown", "behavioral")]
    q.sort(key=lambda w: (any(f in EXCLUDE_FLAGS for f in w["flags"]),
                          -w["as_seller"]["usd"]))
    out["work_queue"] = [{
        "wallet": w["wallet"], "chain": w["chain"], "tier": w["tier"],
        "guess": w["name"] or "", "usd": round(w["as_seller"]["usd"], 2),
        "settlements": w["as_seller"]["settlements"], "buyers": w["as_seller"]["buyers"],
        "flags": w["flags"],
    } for w in q[:50]]
    return out


# ------------------------------------------------------------ persistence --

SCHEMA = """
CREATE TABLE IF NOT EXISTS wallet_labels (
    chain TEXT NOT NULL, wallet TEXT NOT NULL,
    roles TEXT, name TEXT, category TEXT, tier TEXT, evidence TEXT,
    confidence TEXT, flags TEXT,
    seller_settlements INTEGER, seller_usd REAL, seller_buyers INTEGER,
    buyer_settlements INTEGER, buyer_usd REAL, buyer_sellers INTEGER, buyer_class TEXT,
    first_seen TEXT, last_seen TEXT, labelled_at TEXT,
    PRIMARY KEY (chain, wallet)
);
CREATE TABLE IF NOT EXISTS payment_labels (
    event_key TEXT PRIMARY KEY, chain TEXT NOT NULL,
    leg_type TEXT, category TEXT, account TEXT, tier TEXT,
    seller_name TEXT, flags TEXT, labelled_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_payment_labels_tier ON payment_labels(chain, tier);
"""


def persist(conn, result: dict, chains: List[str], payments: bool = True):
    """Write labels into the ledger db.

    payments=False skips the per-payment table: it is fully derivable from
    events + wallet_labels in about a second, and it would nearly double the
    size of a database that is committed to git every six hours.
    """
    now = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
    conn.executescript(SCHEMA)
    for c in chains:
        conn.execute("DELETE FROM wallet_labels WHERE chain = ?", (c,))
        conn.execute("DELETE FROM payment_labels WHERE chain = ?", (c,))
    rows = []
    for w in result["wallets"].values():
        if w["chain"] not in chains:
            continue
        s, b = w.get("as_seller") or {}, w.get("as_buyer") or {}
        firsts = [x for x in (s.get("first_seen"), b.get("first_seen")) if x]
        lasts = [x for x in (s.get("last_seen"), b.get("last_seen")) if x]
        rows.append((w["chain"], w["wallet"], ",".join(w["roles"]), w["name"],
                     w["category"], w["tier"], w["evidence"], w["confidence"],
                     ",".join(w["flags"]), s.get("settlements"), s.get("usd"),
                     s.get("buyers"), b.get("settlements"), b.get("usd"),
                     b.get("sellers"), b.get("class"),
                     min(firsts) if firsts else None, max(lasts) if lasts else None, now))
    conn.executemany("INSERT OR REPLACE INTO wallet_labels VALUES "
                     "(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
    if not payments:
        conn.commit()
        conn.execute("VACUUM")
        return
    conn.executemany(
        "INSERT OR REPLACE INTO payment_labels VALUES (?,?,?,?,?,?,?,?,?)",
        [(p["event_key"], p["chain"], p["leg_type"], p["category"], p["account"],
          p["tier"], p["seller_name"], ",".join(p["flags"]), now)
         for p in result["payments"] if p["chain"] in chains])
    conn.commit()


def public_labels(result: dict) -> dict:
    """The publishable label set: sellers, facilitators, x402 contracts.

    Buyers are left out on purpose - the registry is sellers-only. (Their
    settlements are public on-chain; Counterra simply does not compile a
    named index of who the agents are.)
    """
    out = []
    for w in result["wallets"].values():
        roles = set(w["roles"])
        if not roles & {"seller", "facilitator", "x402-contract"}:
            continue
        item = {k: w[k] for k in ("wallet", "chain", "name", "category", "tier",
                                  "evidence", "confidence", "flags")}
        item["roles"] = [r for r in w["roles"] if r != "buyer"]
        if "as_seller" in w:
            item["as_seller"] = w["as_seller"]
        out.append(item)
    out.sort(key=lambda w: (TIER_ORDER.index(w["tier"]),
                            -((w.get("as_seller") or {}).get("usd") or 0)))
    return {
        "$schema_note": ("Counterra network label book: every seller, facilitator and "
                         "x402 contract seen in the ledger, with an evidence tier. "
                         "Tiers: hard-stop > verified > protocol > probable > "
                         "behavioral > unknown. 'behavioral' is a spend TYPE inferred "
                         "from settlement pattern, never an identity. Buyers are not "
                         "published. Regenerate: counterra.py label --write"),
        "version": 1,
        "generated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
        "wallets": out,
    }


def run_labels(chain: Optional[str] = None, write: bool = False,
               db_path: Optional[str] = None, payments: bool = False) -> dict:
    """CLI entry: label the accumulated ledger; optionally persist + publish."""
    from counterralib.store import EventStore
    import yaml
    store = EventStore(db_path) if db_path else EventStore()
    try:
        events = store.all_events(chain)
        cfg = {}
        try:
            cfg = yaml.safe_load(open(os.path.join(HERE, "config.yaml"))) or {}
        except Exception:
            pass
        res = build(events,
                    registry=_load_json(os.path.join(HERE, "docs", "providers.json"), {}),
                    pending=_load_json(os.path.join(HERE, "docs", "pending-providers.json"), {}),
                    census=fac_mod.load_census(),
                    accounting=cfg.get("accounting"))
        res["chain_scope"] = chain or "all"
        if write:
            chains = [chain] if chain else sorted({e.chain for e in events})
            persist(store.conn, res, chains, payments=payments)
            if not chain:
                # the public files describe the whole network, never a slice
                with open(LABELS_PATH, "w") as f:
                    json.dump(public_labels(res), f, indent=1)
                    f.write("\n")
                cov = dict(res["coverage"])
                cov["generated_at"] = datetime.datetime.now(
                    datetime.timezone.utc).isoformat(timespec="seconds")
                cov["$schema_note"] = ("Counterra label coverage. 'labelled' = any evidence "
                                       "tier; 'named' = verified or probable identity. "
                                       "'adjusted' excludes payments flagged as possible "
                                       "wash/test (self-payment, dust, round-trip); "
                                       "closed-loop is flagged for review but not excluded.")
                with open(COVERAGE_PATH, "w") as f:
                    json.dump(cov, f, indent=1)
                    f.write("\n")
        return res
    finally:
        store.close()


def render_summary(res: dict, top: int = 15) -> str:
    cov = res["coverage"]
    L = []
    for scope in [k for k in cov if k not in ("wallets", "work_queue")]:
        c = cov[scope]
        L.append("{:<8} {:>7,} payments  ${:>13,.2f}   labelled {:>5}% of $ ({:>5}% of payments)   "
                 "named {:>5}% of $".format(
                     scope, c["payments"]["settlements"], c["payments"]["usd"],
                     c["labelled_pct"]["usd"], c["labelled_pct"]["settlements"],
                     c["named_pct"]["usd"]))
        if c["flagged"]["settlements"]:
            L.append("         adjusted (minus {:,} self/dust/round-trip payments, ${:,.2f}): "
                     "${:,.2f}, labelled {}% of $".format(
                         c["flagged"]["settlements"], c["flagged"]["usd"],
                         c["adjusted"]["usd"], c["adjusted_labelled_pct"]["usd"]))
    L.append("")
    L.append("by evidence tier (all chains):")
    for t, v in cov["overall"]["by_tier"].items():
        L.append("  {:<11} {:>7,} payments  ${:>13,.2f}".format(t, v["settlements"], v["usd"]))
    L.append("")
    L.append("wallets:")
    for role, tiers in sorted(cov["wallets"].items()):
        L.append("  {:<14} ".format(role) + ", ".join(
            "{} {}".format(n, t) for t, n in sorted(
                tiers.items(), key=lambda kv: TIER_ORDER.index(kv[0])
                if kv[0] in TIER_ORDER else 99)))
    if cov["work_queue"] and top:
        L.append("")
        L.append("identification work queue (unnamed sellers, by $):")
        for w in cov["work_queue"][:top]:
            L.append("  {:<46} {:<7} ${:>11,.2f} {:>6} pays {:>4} buyers  {}{}".format(
                w["wallet"], w["chain"], w["usd"], w["settlements"], w["buyers"],
                w["guess"] or "unknown",
                "  [" + ",".join(w["flags"]) + "]" if w["flags"] else ""))
    return "\n".join(L)


def write_payments_csv(payments: List[dict], path: str) -> int:
    """Every categorised payment as a flat CSV (opens in Excel / Sheets)."""
    import csv
    cols = ["ts", "chain", "tx_hash", "payer", "payee", "amount_usdc", "leg_type",
            "facilitator", "seller_name", "category", "tier", "account", "flags"]
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(cols)
        for p in payments:
            w.writerow([",".join(p[c]) if c == "flags" else p[c] for c in cols])
    return len(payments)
