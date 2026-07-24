"""
Counterra ledger engine (config-driven).

Takes canonical PaymentEvents plus mapping config and produces:
  - per-agent / per-provider / per-category spend summaries
  - materiality-based aggregated journal entries
  - an exception queue (unmapped counterparties, anomalous amounts)
"""
from collections import defaultdict
import csv

DEFAULT_ACCOUNTING = {
    "asset_account": "1085 - Digital Assets (USDC)",
    "anomaly_threshold_usd": 40.0,
    "expense_accounts": {"Uncategorized": "6490 - Uncategorized Agent Spend"},
}


def enrich(events, agent_map=None, provider_map=None):
    agent_map = agent_map or {}
    provider_map = {k.lower(): v for k, v in (provider_map or {}).items()}
    agent_map = {k.lower(): v for k, v in agent_map.items()}
    rows = []
    for e in events:
        agent = agent_map.get(e.payer_wallet.lower(), e.payer_wallet[:10] + "…")
        p = provider_map.get(e.payee_wallet.lower())
        if p is None:
            label, cat = e.payee_wallet[:10] + "…", "Uncategorized"
        elif isinstance(p, dict):
            label, cat = p.get("label", "?"), p.get("category", "Uncategorized")
        else:
            label, cat = p
        rows.append({**e.to_dict(), "agent": agent, "provider": label, "category": cat})
    return rows


def attribution_summary(rows):
    """
    Aggregate ERC-8021 attribution captured on settlements, for reporting.

    Counterra decodes builder-code suffixes from settlement calldata, giving
    three attribution dimensions the raw transfer does not:

      app_code          which application served the paid endpoint (seller side)
      facilitator_code  which facilitator settled the payment on-chain
      service_codes     which client/agent-side code initiated it

    These are captured on every PaymentEvent but were previously invisible in
    the output. Returns dicts of {code: {'amount', 'settlements'}} plus the
    coverage ratio, since attribution is opt-in and partial by nature — a low
    coverage number is normal and should be shown, not hidden.
    """
    apps, facs, svcs = {}, {}, {}
    n_with_app = n_with_fac = n_with_svc = 0
    for r in rows:
        amt = r.get("amount_usdc", 0.0)
        a = r.get("app_code") or ""
        f = r.get("facilitator_code") or ""
        s = r.get("service_codes") or ""
        if a:
            n_with_app += 1
            d = apps.setdefault(a, {"amount": 0.0, "settlements": 0})
            d["amount"] += amt; d["settlements"] += 1
        if f:
            n_with_fac += 1
            d = facs.setdefault(f, {"amount": 0.0, "settlements": 0})
            d["amount"] += amt; d["settlements"] += 1
        if s:
            n_with_svc += 1
            for one in [x for x in s.split(",") if x]:
                d = svcs.setdefault(one, {"amount": 0.0, "settlements": 0})
                d["amount"] += amt; d["settlements"] += 1
    total = len(rows) or 1
    return {
        "apps": apps, "facilitators": facs, "services": svcs,
        "total_rows": len(rows),
        "coverage": {
            "app": round(n_with_app / total, 4),
            "facilitator": round(n_with_fac / total, 4),
            "service": round(n_with_svc / total, 4),
        },
    }


def summarize(rows):
    by_agent, by_provider, by_category, by_day = (defaultdict(float) for _ in range(4))
    tx_by_agent = defaultdict(int)
    for r in rows:
        by_agent[r["agent"]] += r["amount_usdc"]
        tx_by_agent[r["agent"]] += 1
        by_provider[r["provider"]] += r["amount_usdc"]
        by_category[r["category"]] += r["amount_usdc"]
        by_day[r["ts"][:10]] += r["amount_usdc"]
    return {
        "by_agent": dict(by_agent),
        "tx_by_agent": dict(tx_by_agent),
        "by_provider": dict(by_provider),
        "by_category": dict(by_category),
        "by_day": dict(sorted(by_day.items())),
        "total": sum(by_agent.values()),
        "n_events": len(rows),
    }


def journal_entries(rows, period, accounting=None):
    acc = {**DEFAULT_ACCOUNTING, **(accounting or {})}
    exp = acc["expense_accounts"]
    agg = defaultdict(lambda: {"amount": 0.0, "disposals": 0})
    for r in rows:
        if not r["ts"].startswith(period):
            continue
        key = (r["payee_wallet"], r["provider"], r["category"])
        agg[key]["amount"] += r["amount_usdc"]
        agg[key]["disposals"] += 1
    entries = []
    for (wallet, provider, cat), v in sorted(agg.items(), key=lambda kv: -kv[1]["amount"]):
        amt = round(v["amount"], 2)
        if amt < 0.01:
            continue
        entries.append({
            "period": period,
            "debit_account": exp.get(cat, exp.get("Uncategorized", "6490")),
            "credit_account": acc["asset_account"],
            "amount_usd": amt,
            "provider": provider,
            "provider_wallet": wallet,
            "category": cat,
            "settlements": v["disposals"],
            "memo": f"Agent spend {period} - {provider} ({v['disposals']} settlements aggregated)",
        })
    return entries


def exceptions(rows, accounting=None):
    acc = {**DEFAULT_ACCOUNTING, **(accounting or {})}
    thr = float(acc.get("anomaly_threshold_usd", 40.0))
    out = []
    for r in rows:
        if r["category"] == "Uncategorized":
            out.append({**r, "reason": "Unmapped counterparty - needs classification"})
        elif r["amount_usdc"] >= thr:
            out.append({**r, "reason": f"Amount >= ${thr:.0f} - review"})
    return out


def grouped_exceptions(rows, accounting=None):
    """
    Exceptions collapsed to one row per underlying problem.

    `exceptions()` returns one entry per settlement, which inflates the count
    and makes the queue unactionable: 131 settlements to a single unmapped
    counterparty is ONE thing to fix, not 131. Anomalies (single large
    payments) stay per-settlement, because each is individually reviewable.

    Returns rows sorted by amount, each with: reason, counterparty, provider,
    settlements, amount_usdc, first_ts, last_ts, distinct_payers.
    """
    acc = {**DEFAULT_ACCOUNTING, **(accounting or {})}
    thr = float(acc.get("anomaly_threshold_usd", 40.0))
    unmapped = {}
    anomalies = []
    for r in rows:
        if r["amount_usdc"] >= thr:
            anomalies.append({
                "reason": f"Amount >= ${thr:.0f} - review",
                "counterparty": r["payee_wallet"],
                "provider": r["provider"],
                "settlements": 1,
                "amount_usdc": r["amount_usdc"],
                "first_ts": r["ts"], "last_ts": r["ts"],
                "distinct_payers": 1,
            })
        elif r["category"] == "Uncategorized":
            w = r["payee_wallet"].lower()
            g = unmapped.setdefault(w, {
                "reason": "Unmapped counterparty - needs classification",
                "counterparty": r["payee_wallet"],
                "provider": r["provider"],
                "settlements": 0, "amount_usdc": 0.0,
                "first_ts": r["ts"], "last_ts": r["ts"],
                "_payers": set(),
            })
            g["settlements"] += 1
            g["amount_usdc"] += r["amount_usdc"]
            g["_payers"].add(r["payer_wallet"])
            if r["ts"] < g["first_ts"]:
                g["first_ts"] = r["ts"]
            if r["ts"] > g["last_ts"]:
                g["last_ts"] = r["ts"]
    out = []
    for g in unmapped.values():
        g["distinct_payers"] = len(g.pop("_payers"))
        g["amount_usdc"] = round(g["amount_usdc"], 6)
        out.append(g)
    out.extend(anomalies)
    out.sort(key=lambda g: -g["amount_usdc"])
    return out


def write_journal_csv(entries, path):
    if not entries:
        open(path, "w").write("")
        return
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(entries[0].keys()))
        w.writeheader()
        w.writerows(entries)
