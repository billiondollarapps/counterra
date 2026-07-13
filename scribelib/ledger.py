"""
Scribe ledger engine (config-driven).

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
        agg[(r["provider"], r["category"])]["amount"] += r["amount_usdc"]
        agg[(r["provider"], r["category"])]["disposals"] += 1
    entries = []
    for (provider, cat), v in sorted(agg.items(), key=lambda kv: -kv[1]["amount"]):
        amt = round(v["amount"], 2)
        if amt < 0.01:
            continue
        entries.append({
            "period": period,
            "debit_account": exp.get(cat, exp.get("Uncategorized", "6490")),
            "credit_account": acc["asset_account"],
            "amount_usd": amt,
            "provider": provider,
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


def write_journal_csv(entries, path):
    if not entries:
        open(path, "w").write("")
        return
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(entries[0].keys()))
        w.writeheader()
        w.writerows(entries)
