# Counterra

**Financial telemetry for agent payments — see, attribute, and explain every dollar your agents spend. Accounting is the output, not the ask.**

AI agents now pay for data, tools, and compute over machine payment rails like
[x402](https://x402.org) — millions of micropayments with **no invoices, no
receipts, no books**. Counterra reads those payments straight off the chain
(Base + Solana) and turns them into per-agent spend attribution, journal
entries, and exports your accountant can import into QuickBooks or Xero.

*Agents move the money. Counterra makes it count.*

## Get your agent's books in one command

You don't need to configure anything. Point Counterra at an agent wallet and it
auto-detects the chain, sweeps its x402 spend, and produces books:

```bash
pip install pyyaml requests
python3 counterra.py books --wallet 0xYOUR_AGENT_WALLET
```

You get, in `out/`:
- `spend_report_books.html` — open in any browser
- `journal_quickbooks_books.csv` — import into QuickBooks
- `journal_xero_books.csv` — import into Xero

Non-custodial: Counterra reads public ledgers, never holds or moves funds, and
has no token. Nothing leaves your machine.

**Running paying agents and want your books closed?** I'm onboarding 2–3 design
partners free, in exchange for feedback — open an issue or reach out.

## Try it live — no install

**[counterra.xyz](https://counterra.xyz)** — paste any Base wallet and watch
Counterra close its books on real x402 traffic, in your browser.

## The standard

Counterra authors **[CAAP-1](spec/CAAP-1.md)** — the open standard for turning
an x402 settlement (and optional [x402-receipts](https://github.com/StelarDigital/x402-receipts)
delivery receipt) into a double-entry accounting record: amount normalization,
double-entry mapping, bookability rules, and exception handling. The spec ships
with golden conformance vectors (`spec/caap1_vectors.json`) — any implementation
is CAAP-1 conformant if it reproduces them. x402 standardizes payments;
x402-receipts standardizes delivery; **CAAP-1 standardizes accounting.**

## Operator commands (for running your own ledger)

```bash
python3 counterra.py demo                     # simulated x402 traffic
python3 counterra.py live --continuous --chain base    # sweep + accumulate Base
python3 counterra.py live --continuous --chain solana  # sweep + accumulate Solana
python3 counterra.py status                    # continuous-ingestion progress
python3 counterra.py classify --write          # auto-identify unmapped sellers
python3 counterra.py observed                   # observed demand per registry seller
python3 counterra.py profile <wallet>           # fingerprint an unknown seller
python3 counterra.py codes                       # ERC-8021 builder-code findings
python3 counterra.py receipt <receipt.json>      # consume an x402 receipt into a journal entry
python3 counterra.py whois --url <seller-url>    # resolve a seller's payTo and identify it
```

Live data uses Blockscout's free public API for Base and Solana's public
mainnet RPC — no keys required. For faster Solana sweeps, put a free Helius
endpoint in `.env` as `SOLANA_RPC_URL=...`.

## What it does

- **Ingest** — sweeps settlements submitted by x402 facilitator
  wallets on Base (Coinbase runs ~40 and rotates them; Counterra
  auto-refreshes the list from the community registry), decodes the
  USDC transfers inside: payer (agent) → payee (seller) → amount.
- **Ledger** — attributes spend per agent, aggregates thousands of
  sub-cent events into ERP-ready journal entries
  (Dr expense / Cr Digital Assets), flags unmapped counterparties
  and anomalies into an exception queue.
- **Comply** — retains per-entry settlement counts (each is a
  potential digital-asset disposal for tax purposes). VAT/GST module
  and confidential-rail audit ingestion are on the roadmap.

Counterra is **non-custodial by design**: it reads public ledgers,
never holds or moves funds, and has no token.

## Tests (offline, no key)

```bash
python3 tests/test_live.py
```

Canned API-shaped fixtures verify the full decode path, wallet
filtering, and the facilitator-refresh rewrite.

## Why this exists

The x402 protocol deliberately removed accounts, invoices, and
billing relationships from payments — that is its genius for
machines, and its unsolved liability for the businesses deploying
them. Industry reviewers note that tax and invoicing remain
unaddressed at the protocol level, and enterprises name the
audit/accountability gap as the blocker for autonomous transactions.
Every rail is bundling reporting for its own rail; nobody owns the
neutral layer across rails. Counterra is that layer, built in the open.

## Roadmap

- [x] Base collector (facilitator sweep + wallet tracking), live-verified
- [x] Solana collector — facilitator sweep + wallet tracking (Coinbase + PayAI), live
- [x] Agentic subledger: attribution, aggregation, exceptions
- [x] Continuous ingestion — persistent SQLite ledger accumulates across runs; scheduled worker sweeps both chains every 6h
- [x] Facilitator auto-refresh from the x402scan community registry
- [x] Auto-classification — `classify [--write]`: batch-identifies unmapped sellers into the registry
- [x] Open seller-mapping registry — `docs/providers.json`, served at counterra.xyz/providers.json; evidence-required contributions
- [x] ERC-8021 builder-code decoding — per-app attribution from settlement calldata
- [x] QuickBooks/Xero journal exports
- [x] x402-receipts consumer — delivery-enriched journal entries
- [x] CAAP-1 spec + golden conformance vectors — the accounting standard
- [x] Observed-demand scoring + seller fingerprinting
- [x] One-command design-partner handoff — `books --wallet`
- [ ] Receipt/evidence alignment via x402 Foundation process (issue #2833)
- [ ] Public "agent spend explorer" (paste a wallet, get books, in-browser)
- [ ] VAT/GST & disposal tax module
- [ ] Confidential-rail audit ingestion (viewing keys)

## Repo map

```
counterra.py              CLI (books / live / demo / classify / observed / profile / receipt / whois / codes / status)
counterralib/ingest.py    canonical PaymentEvent + sample generator
counterralib/live.py      Base adapter (Blockscout/Etherscan) + facilitator refresh
counterralib/solana.py    Solana SPL-USDC adapter
counterralib/store.py     persistent SQLite event store (running ledger)
counterralib/continuous.py  accumulation orchestration
counterralib/ledger.py    attribution, aggregation, journal entries, exceptions
counterralib/exports.py   QuickBooks + Xero exporters
counterralib/receipts.py  x402-receipts v0.3 consumer (CAAP-1 reference impl)
counterralib/erc8021.py   ERC-8021 builder-code decoder
counterralib/observed.py  observed-demand evidence
counterralib/profile.py   seller fingerprinting
counterralib/buildercodes.py  builder-code registry evidence
counterralib/resolve.py   URL -> payTo resolver
counterralib/books.py     one-command design-partner handoff
counterralib/whois.py     seller identification
report.py                 HTML report
spec/CAAP-1.md            the accounting standard + spec/caap1_vectors.json
config.yaml               chains, facilitators, maps, chart of accounts
tests/                    offline test suite (14 suites)
```

## License

Apache-2.0. Open core; use it, fork it, build on it.
