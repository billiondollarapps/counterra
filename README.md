# Scribe (working title)

**The open accounting & audit layer for agentic commerce.**
AI agents pay for data, tools, and compute over machine payment rails
(x402/AP2) — thousands of micropayments with no invoices, no receipts,
no books. Scribe ingests those payments and produces what a finance
team needs: per-agent spend attribution, aggregated journal entries,
tax-relevant disposal counts, and an exception queue.

*Agents move the money. Scribe makes it count.*

## Quick start

```bash
pip install pyyaml requests
python3 scribe.py demo        # simulated x402 traffic -> out/spend_report.html
```

## Live data (10-minute setup)

1. Create a **free API key** at https://etherscan.io/apis
   (Etherscan V2 keys cover Base via chainid 8453).
2. Create a file named `.env` next to `scribe.py`:
   ```
   ETHERSCAN_API_KEY=your_key_here
   ```
3. Run a real sweep of x402 settlements on Base:
   ```bash
   python3 scribe.py live --limit 150
   ```
   This pulls recent settlements submitted by Coinbase's x402
   facilitator wallets (see `config.yaml`), decodes the USDC
   transfers (agent -> seller), and closes the books on them.

4. Or track a specific payer wallet's spend:
   ```bash
   python3 scribe.py live --wallet 0xYourAgentWallet
   ```

Unknown sellers land in the exception queue; map them in
`config.yaml` under `providers:` and re-run to see them
categorized into proper expense accounts.

## How live ingestion works
x402 settlements are submitted on-chain BY facilitator wallets
(Basescan labels them "Coinbase: x402 Facilitator N"; Facilitator 8
alone has ~2.5M transactions). Scribe lists their recent transactions,
keeps the ones calling the USDC contract, and decodes the ERC-20
Transfer inside: payer (agent) -> payee (seller) -> amount.
Public data, no permissions needed.

## Tests (no API key required)
```bash
python3 tests/test_live.py
```
Feeds the live adapter canned Etherscan-shaped responses and verifies
the full decode path.

## Repo map
- `scribe.py` — CLI (demo / live sweep / wallet tracking)
- `scribelib/ingest.py` — canonical PaymentEvent + sample generator
- `scribelib/live.py` — Base-chain adapter (Etherscan V2)
- `scribelib/ledger.py` — attribution, aggregation, journal entries, exceptions
- `report.py` — HTML monthly-close report
- `config.yaml` — chain, facilitators, agent/provider maps, chart of accounts

## Roadmap (grant milestones)
- M1–M3: receipt/evidence alignment (TrustBench-compatible) + live collectors (Base, Solana)
- M4–M6: per-agent subledger + QuickBooks/Xero journal sync, design partners
- M7–M9: VAT/GST & disposal tax module + confidential-rail audit ingestion
