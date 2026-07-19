# Contributing to the Counterra Seller Registry

The open seller-mapping registry lives at `docs/providers.json` and is
served publicly at https://counterra.xyz/providers.json. Every entry
maps an x402 seller wallet to a verified identity so anyone's books
classify correctly.

## Rules (non-negotiable)

1. **Sellers only — never agents.** Sellers publicly advertise
   services with payment addresses; listing them is directory work.
   Payer/agent wallets are private operations and are never mapped
   here. (Label your own agents privately in your config.)
2. **Evidence required.** Every entry must state *how* the mapping
   was verified, in a way a reviewer can reproduce — e.g. a
   facilitator discovery-catalog payTo match (run
   `python3 counterra.py whois <wallet>`), or the seller's own
   public documentation. No evidence, no merge.
3. **Case matters.** EVM wallets lowercase; Solana base58 exact.

## How to submit

Run `python3 counterra.py whois <wallet>` — it prints a ready-to-paste
JSON entry when it finds catalog evidence. Add it to
`docs/providers.json` and open a pull request. One entry per PR keeps
review fast.

## Corrections

Sellers rotate wallets and rebrand. PRs correcting stale entries are
as valuable as new ones — include evidence for the change.
