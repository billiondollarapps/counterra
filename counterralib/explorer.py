"""
x402 network explorer - the public, browsable database behind counterra.xyz/explorer.

Turns the running ledger (out/counterra.db) plus the label book into static
JSON + CSV files under docs/data/, which GitHub Pages serves for free. No
server, no hosted database: the bot regenerates them every run.

  docs/data/network.json            network totals, daily series, categories,
                                    facilitators, label coverage
  docs/data/sellers.json            every seller seen: name/tier/category,
                                    revenue (all / 30d / 7d), buyers, activity
  docs/data/sellers/<wallet>.json   one seller: daily revenue, price ladder,
                                    facilitators, top buyers, recent payments
  docs/data/revenue/<wallet>.csv    one seller's x402 REVENUE LEDGER: every
                                    observed payment + monthly journal entries
                                    (Dr 1085 Digital Assets / Cr 4100 x402
                                    Revenue) - the thing to hand a seller

Honest scope, stated on every file: this is OBSERVED volume. Base is sampled
(the free RPC tier covers roughly one block range in ten), Solana is swept
from known facilitators. Every number is a floor, not a census.

Buyers appear only as wallet addresses (public on-chain data); Counterra
never attaches names to buyers. Python 3.8-safe.
"""

from __future__ import annotations

import csv
import datetime
import json
import os
import re
import shutil
from collections import Counter, defaultdict
from typing import Dict, List, Optional

from counterralib import labels as L

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(HERE, "docs", "data")

DETAIL_TOP = 300          # sellers with a detail page (by observed revenue)
REVENUE_TOP = 150         # sellers with a downloadable revenue ledger
RECENT_N = 100
TOP_BUYERS = 25

REVENUE_ACCOUNT = "4100 - x402 Revenue"
ASSET_ACCOUNT = "1085 - Digital Assets (USDC)"

SCOPE_NOTE = ("Observed x402 settlements only. Base is sampled from the chain "
              "(free RPC tier: a fraction of all blocks); Solana is swept from "
              "known facilitators. Every figure is a floor, not a census.")

SAFE = re.compile(r"[^0-9A-Za-z]")


def slug(wallet: str) -> str:
    return SAFE.sub("", wallet)[:64]


def _day(ts: str) -> str:
    return ts[:10]


def _r(x, n=2):
    return round(float(x), n)


def build(payments: List[dict], wallets: Dict[str, dict], coverage: dict,
          now: Optional[datetime.datetime] = None) -> dict:
    """Pure function: labelled payments -> explorer datasets (dicts/rows)."""
    now = now or datetime.datetime.now(datetime.timezone.utc)
    d7 = (now - datetime.timedelta(days=7)).isoformat()
    d30 = (now - datetime.timedelta(days=30)).isoformat()

    pay = [p for p in payments if p["leg_type"] in L.PAYMENT_LEGS and not L.excluded(p)]

    # ---------------- network ----------------
    daily = defaultdict(lambda: {"usd": 0.0, "n": 0, "buyers": set(), "sellers": set()})
    cats = defaultdict(lambda: {"usd": 0.0, "n": 0})
    facs = defaultdict(lambda: {"usd": 0.0, "n": 0})
    chains = defaultdict(lambda: {"usd": 0.0, "n": 0, "buyers": set(), "sellers": set(),
                                  "first": None, "last": None})
    for p in pay:
        k = (p["chain"], _day(p["ts"]))
        d = daily[k]
        d["usd"] += p["amount_usdc"]; d["n"] += 1
        d["buyers"].add(p["payer"]); d["sellers"].add(p["payee"])
        c = chains[p["chain"]]
        c["usd"] += p["amount_usdc"]; c["n"] += 1
        c["buyers"].add(p["payer"]); c["sellers"].add(p["payee"])
        c["first"] = min(c["first"] or p["ts"], p["ts"])
        c["last"] = max(c["last"] or p["ts"], p["ts"])
        cat = p["category"] or L.UNCATEGORIZED
        cats[cat]["usd"] += p["amount_usdc"]; cats[cat]["n"] += 1
        f = p.get("facilitator") or "unattributed"
        facs[(p["chain"], f)]["usd"] += p["amount_usdc"]; facs[(p["chain"], f)]["n"] += 1

    cov = coverage.get("overall", {})
    network = {
        "generated_at": now.isoformat(timespec="seconds"),
        "scope_note": SCOPE_NOTE,
        "chains": {c: {"usd": _r(v["usd"]), "settlements": v["n"],
                       "buyers": len(v["buyers"]), "sellers": len(v["sellers"]),
                       "first_seen": v["first"], "last_seen": v["last"]}
                   for c, v in sorted(chains.items())},
        "totals": {"usd": _r(sum(v["usd"] for v in chains.values())),
                   "settlements": sum(v["n"] for v in chains.values()),
                   "buyers": len(set().union(*[v["buyers"] for v in chains.values()]) if chains else set()),
                   "sellers": len(set().union(*[v["sellers"] for v in chains.values()]) if chains else set())},
        "daily": [{"chain": c, "date": day, "usd": _r(v["usd"]), "settlements": v["n"],
                   "buyers": len(v["buyers"]), "sellers": len(v["sellers"])}
                  for (c, day), v in sorted(daily.items(), key=lambda kv: (kv[0][1], kv[0][0]))],
        "categories": sorted([{"category": k, "usd": _r(v["usd"]), "settlements": v["n"]}
                              for k, v in cats.items()], key=lambda r: -r["usd"]),
        "facilitators": sorted([{"chain": c, "facilitator": f, "usd": _r(v["usd"]),
                                 "settlements": v["n"]} for (c, f), v in facs.items()],
                               key=lambda r: -r["usd"]),
        "coverage": {
            "labelled_pct_usd": (cov.get("labelled_pct") or {}).get("usd"),
            "named_pct_usd": (cov.get("named_pct") or {}).get("usd"),
            "labelled_pct_settlements": (cov.get("labelled_pct") or {}).get("settlements"),
            "wallets": coverage.get("wallets", {}),
        },
    }

    # ---------------- sellers ----------------
    by_seller = defaultdict(list)
    for p in pay:
        by_seller[p["payee"]].append(p)
    sellers = []
    for w, ps in by_seller.items():
        meta = wallets.get(w, {})
        usd = sum(p["amount_usdc"] for p in ps)
        sellers.append({
            "wallet": w, "chain": ps[0]["chain"], "slug": slug(w),
            "name": meta.get("name") or "", "tier": meta.get("tier", "unknown"),
            "category": meta.get("category") or L.UNCATEGORIZED,
            "usd": _r(usd), "usd_30d": _r(sum(p["amount_usdc"] for p in ps if p["ts"] >= d30)),
            "usd_7d": _r(sum(p["amount_usdc"] for p in ps if p["ts"] >= d7)),
            "settlements": len(ps), "buyers": len({p["payer"] for p in ps}),
            "first_seen": min(p["ts"] for p in ps), "last_seen": max(p["ts"] for p in ps),
            "median_price": _r(sorted(p["amount_usdc"] for p in ps)[len(ps) // 2], 6),
            "flags": meta.get("flags") or [],
        })
    sellers.sort(key=lambda s: -s["usd"])
    named = {s["wallet"] for s in sellers if s["tier"] in ("verified", "probable")}
    detail_set = {s["wallet"] for s in sellers[:DETAIL_TOP]} | named
    revenue_set = {s["wallet"] for s in sellers[:REVENUE_TOP]} | named
    for s in sellers:
        s["has_detail"] = s["wallet"] in detail_set
        s["has_revenue_csv"] = s["wallet"] in revenue_set

    details, ledgers = {}, {}
    for s in sellers:
        w = s["wallet"]
        if w not in detail_set:
            continue
        ps = sorted(by_seller[w], key=lambda p: p["ts"])
        dd = defaultdict(lambda: {"usd": 0.0, "n": 0, "buyers": set()})
        for p in ps:
            x = dd[_day(p["ts"])]
            x["usd"] += p["amount_usdc"]; x["n"] += 1; x["buyers"].add(p["payer"])
        bu = defaultdict(lambda: {"usd": 0.0, "n": 0, "last": ""})
        for p in ps:
            b = bu[p["payer"]]
            b["usd"] += p["amount_usdc"]; b["n"] += 1; b["last"] = max(b["last"], p["ts"])
        ladder = Counter(_r(p["amount_usdc"], 6) for p in ps)
        fz = Counter(p.get("facilitator") or "unattributed" for p in ps)
        legs = Counter(p["leg_type"] for p in ps)
        meta = wallets.get(w, {})
        details[s["slug"]] = {
            "generated_at": network["generated_at"], "scope_note": SCOPE_NOTE,
            "seller": s, "evidence": meta.get("evidence", ""),
            "confidence": meta.get("confidence", ""), "roles": meta.get("roles", []),
            "daily": [{"date": k, "usd": _r(v["usd"], 4), "settlements": v["n"],
                       "buyers": len(v["buyers"])} for k, v in sorted(dd.items())],
            "price_ladder": [{"price": k, "settlements": n} for k, n in ladder.most_common(12)],
            "facilitators": [{"facilitator": k, "settlements": n} for k, n in fz.most_common()],
            "settlement_paths": dict(legs),
            "top_buyers": [{"wallet": k, "usd": _r(v["usd"], 4), "settlements": v["n"],
                            "last_seen": v["last"]}
                           for k, v in sorted(bu.items(), key=lambda kv: -kv[1]["usd"])[:TOP_BUYERS]],
            "buyer_concentration_pct": (_r(100.0 * max(v["usd"] for v in bu.values()) / s["usd"], 1)
                                        if s["usd"] else 0.0),
            "recent": [{"ts": p["ts"], "tx": p["tx_hash"], "buyer": p["payer"],
                        "usd": _r(p["amount_usdc"], 6), "path": p["leg_type"],
                        "facilitator": p.get("facilitator") or ""}
                       for p in ps[-RECENT_N:][::-1]],
        }
        if w in revenue_set:
            ledgers[s["slug"]] = ps
    return {"network": network, "sellers": sellers, "details": details, "ledgers": ledgers}


def revenue_rows(seller: dict, ps: List[dict]):
    """Rows of one seller's revenue ledger CSV: header block, monthly journal
    entries (what an accountant books), then every observed payment."""
    rows = [["# Counterra x402 revenue ledger"],
            ["# seller", seller["wallet"], seller["chain"], seller.get("name") or "unidentified"],
            ["# scope", SCOPE_NOTE],
            ["# generated", datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")],
            [],
            ["section", "period", "debit_account", "credit_account", "amount_usd",
             "settlements", "buyers", "memo"]]
    months = defaultdict(lambda: {"usd": 0.0, "n": 0, "buyers": set()})
    for p in ps:
        m = months[p["ts"][:7]]
        m["usd"] += p["amount_usdc"]; m["n"] += 1; m["buyers"].add(p["payer"])
    for period, m in sorted(months.items()):
        rows.append(["journal", period, ASSET_ACCOUNT, REVENUE_ACCOUNT, "%.6f" % m["usd"],
                     m["n"], len(m["buyers"]),
                     "x402 revenue {} ({} settlements aggregated)".format(period, m["n"])])
    rows += [[], ["section", "timestamp_utc", "tx_hash", "buyer_wallet", "amount_usdc",
                  "settlement_path", "facilitator", "chain"]]
    for p in ps:
        rows.append(["payment", p["ts"], p["tx_hash"], p["payer"], "%.6f" % p["amount_usdc"],
                     p["leg_type"], p.get("facilitator") or "", p["chain"]])
    return rows


def write(out: dict, data_dir: Optional[str] = None) -> dict:
    d = data_dir or DATA_DIR
    # rebuild per-seller folders from scratch so dropped sellers disappear
    for sub in ("sellers", "revenue"):
        p = os.path.join(d, sub)
        if os.path.isdir(p):
            shutil.rmtree(p)
        os.makedirs(p, exist_ok=True)

    def dump(path, obj):
        with open(path, "w") as f:
            json.dump(obj, f, separators=(",", ":"))

    dump(os.path.join(d, "network.json"), out["network"])
    dump(os.path.join(d, "sellers.json"), {"generated_at": out["network"]["generated_at"],
                                           "scope_note": SCOPE_NOTE,
                                           "sellers": out["sellers"]})
    for sl, det in out["details"].items():
        dump(os.path.join(d, "sellers", sl + ".json"), det)
    by_slug = {s["slug"]: s for s in out["sellers"]}
    for sl, ps in out["ledgers"].items():
        with open(os.path.join(d, "revenue", sl + ".csv"), "w", newline="") as f:
            csv.writer(f).writerows(revenue_rows(by_slug[sl], ps))
    return {"sellers": len(out["sellers"]), "details": len(out["details"]),
            "ledgers": len(out["ledgers"])}


def run(write_files: bool = False, db_path: Optional[str] = None,
        data_dir: Optional[str] = None) -> dict:
    res = L.run_labels(write=False, db_path=db_path)
    out = build(res["payments"], res["wallets"], res["coverage"])
    counts = write(out, data_dir) if write_files else {
        "sellers": len(out["sellers"]), "details": len(out["details"]),
        "ledgers": len(out["ledgers"])}
    return {"out": out, "counts": counts}


def render_summary(result: dict) -> str:
    n = result["out"]["network"]
    L_ = ["x402 explorer dataset ({})".format(n["generated_at"])]
    for c, v in n["chains"].items():
        L_.append("  {:<7} ${:>12,.2f}  {:>7,} payments  {:>5,} buyers  {:>4,} sellers  {} .. {}".format(
            c, v["usd"], v["settlements"], v["buyers"], v["sellers"],
            (v["first_seen"] or "")[:10], (v["last_seen"] or "")[:10]))
    c = result["counts"]
    L_.append("  {} sellers indexed, {} seller pages, {} revenue ledgers".format(
        c["sellers"], c["details"], c["ledgers"]))
    L_.append("  top sellers by observed revenue:")
    for s in result["out"]["sellers"][:8]:
        L_.append("    {:<46} {:<7} ${:>10,.2f} {:>6} pays {:>5} buyers  {}".format(
            s["wallet"], s["chain"], s["usd"], s["settlements"], s["buyers"],
            s["name"] or "(unidentified)"))
    return "\n".join(L_)
