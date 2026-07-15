"""
Counterra CLI - agents move the money, Counterra makes it count.

Usage:
  python3 counterra.py demo                     # simulated x402 traffic
  python3 counterra.py live --limit 150         # sweep real facilitator settlements (needs API key)
  python3 counterra.py live --wallet 0xABC...   # track one payer wallet's real spend
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import yaml

from counterralib.ledger import enrich, summarize, journal_entries, exceptions, write_journal_csv
from report import render  # shared HTML renderer

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "out")


def load_config():
    with open(os.path.join(HERE, "config.yaml")) as f:
        return yaml.safe_load(f)


def load_env():
    envp = os.path.join(HERE, ".env")
    if os.path.exists(envp):
        for line in open(envp):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())


def latest_full_period(rows):
    """Most recent YYYY-MM present in the data."""
    months = sorted({r["ts"][:7] for r in rows})
    return months[-1] if months else "----"


def run(events, cfg, entity_label):
    agent_map = cfg.get("agents") or {}
    provider_map = cfg.get("providers") or {}
    accounting = cfg.get("accounting") or {}
    rows = enrich(events, agent_map, provider_map)
    if not rows:
        print("No payment events found. If running live: check your API key, "
              "or raise --limit (facilitators batch heavily).")
        return
    summary = summarize(rows)
    period = latest_full_period(rows)
    entries = journal_entries(rows, period, accounting)
    exc = exceptions(rows, accounting)
    os.makedirs(OUT, exist_ok=True)
    write_journal_csv(entries, os.path.join(OUT, "journal_entries.csv"))
    with open(os.path.join(OUT, "spend_report.html"), "w") as f:
        f.write(render(summary, entries, exc, period, entity_label))
    print(f"events={len(rows)}  total=${summary['total']:,.2f}  "
          f"period={period}  journal_entries={len(entries)}  exceptions={len(exc)}")
    print("outputs: out/spend_report.html, out/journal_entries.csv")


def main():
    ap = argparse.ArgumentParser(description="Counterra - accounting for agentic commerce")
    sub = ap.add_subparsers(dest="mode", required=True)
    sub.add_parser("demo", help="run on simulated x402 traffic")
    sub.add_parser("refresh", help="update facilitator wallets from the x402scan registry")
    wh = sub.add_parser("whois", help="identify a seller wallet via Bazaar + Blockscout")
    wh.add_argument("address", help="the payee wallet to identify")
    lv = sub.add_parser("live", help="run on real Base-chain x402 data")
    lv.add_argument("--limit", type=int, default=150, help="settlements to sweep")
    lv.add_argument("--wallet", type=str, default=None, help="track one payer wallet")
    args = ap.parse_args()

    if args.mode == "whois":
        from counterralib.whois import whois
        whois(args.address)
        return

    if args.mode == "refresh":
        from counterralib.live import refresh_facilitators
        refresh_facilitators(os.path.join(HERE, "config.yaml"))
        return

    cfg = load_config()
    if args.mode == "demo":
        from counterralib.ingest import SampleDataAdapter
        from run_demo import SAMPLE_AGENTS, SAMPLE_PROVIDERS
        cfg = dict(cfg)
        cfg["agents"] = SAMPLE_AGENTS
        cfg["providers"] = SAMPLE_PROVIDERS
        run(SampleDataAdapter(days=30).fetch(), cfg, "Demo Co (simulated data)")
    else:
        load_env()
        if "etherscan" in (cfg.get("chain", {}).get("api_base") or "") and \
                not os.environ.get("ETHERSCAN_API_KEY"):
            print("Etherscan mode needs ETHERSCAN_API_KEY in .env "
                  "(or switch api_base to Blockscout, which needs no key).")
            sys.exit(1)
        from counterralib.live import BaseChainAdapter
        adapter = BaseChainAdapter(cfg)
        if args.wallet:
            events = adapter.fetch_wallet(args.wallet)
            label = f"Wallet {args.wallet[:10]}… (live Base data)"
        else:
            events = adapter.fetch(limit=args.limit)
            label = "x402 facilitator sweep (live Base data)"
        run(events, cfg, label)


if __name__ == "__main__":
    main()
