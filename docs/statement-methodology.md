# Counterra statement packs — accounting methodology (for review)

This note describes exactly how Counterra turns on-chain x402 settlements into
a monthly close pack, so a qualified accountant can confirm or correct each
treatment. Every assumption that needs professional sign-off is marked
**[REVIEW]**. Nothing here is tax or legal advice.

## 1. Source data

- **What a payment is.** An x402 settlement: a USDC transfer from a buyer's
  wallet to a seller's wallet, submitted on-chain by a payment *facilitator*
  (Coinbase, PayAI, …) after the seller served an HTTP request. Counterra decodes
  these from the public chain (Base via RPC, Solana via RPC).
- **What is not included.** Ordinary wallet transfers, trades, and anything
  that is not an x402 settlement. A pack is an *x402 activity* statement, not a
  full wallet statement, and shows no opening or closing wallet balance.
- **Completeness.** A pack built from a wallet's own sweep
  (`counterra.py books --wallet`) is complete for that wallet. Packs built from
  the shared network ledger (the public demo) can miss payments, because Base is
  sampled.
- **Settlement paths.** `exact` (EIP-3009 transfer), `permit2` (via the x402
  Permit2 proxy) and `batch-payout` (from an x402 batch-settlement channel) are
  all treated as payments. `batch-deposit` (a buyer pre-funding a channel) is
  **not** a payment; see §4.

## 2. Measurement

- **Transaction vs reporting currency (decided).** Every payment is recorded
  in the token that actually moved (USDC) and translated to **USD at that UTC
  day's average rate**, exactly as a rupee or euro transaction is translated
  through forex. The rate is the mean of that day's hourly CoinGecko prices
  (`counterra.py rates --update`, cached in `out/rates.json`). USDC normally
  trades at 0.999–1.001 but has de-pegged (about 0.88 in March 2023), so par is
  never silently assumed. If a day has no rate, the pack translates it at
  1.00 **and lists it as an exception**. Every pack includes the rates it used
  (`rates.csv`) and states the translation difference against par.
  **[REVIEW]** (a) Is the daily average acceptable, or is the spot rate at
  settlement time required? (b) Counterra translates at the transaction date
  only; any exchange gain or loss on USDC still held at period end (re-measured
  at the closing rate) is outside the pack. Who books that remeasurement?
- **Precision.** USDC has 6 decimals and every payment is kept exact. The USD
  total is rounded to cents **once**, and the cents are allocated across
  counterparties with the largest-remainder method, so the lines always sum
  exactly to the rounded total. Rounding is half-up. **[REVIEW]** Is a disclosed
  sub-cent rounding difference acceptable, or should it be posted to a rounding
  account?
- **Timing.** A payment is recognised on its on-chain settlement timestamp
  (UTC). x402 settles at or immediately after the request is served. Months
  follow UTC. **[REVIEW]** Should month boundaries follow the entity's local
  time zone?
- **Fees.** In x402 the facilitator pays network gas, so the buyer pays exactly
  the listed price and the seller receives it in full. There is no fee line.

## 3. Seller packs (revenue)

- **Entry:** Dr `1085 Digital Assets (USDC)` / Cr `4100 x402 Revenue`, in USD
  at daily average rates.
- **Anonymous buyers (decided).** Revenue from a buyer the seller cannot
  identify is still revenue: the service was delivered and paid. It is **not**
  parked in suspense. That would understate revenue, and suspense balances are
  meant to clear. It is booked against one customer, **"Anonymous x402
  customers"**, the same way a shop books cash or walk-in sales. Every wallet
  is kept in a customer sub-ledger (`customer_ledger.csv`).
- **Identified buyers.** A seller can map wallets it knows (API-key accounts,
  contracts) to legal customers (`--customers customers.json`). Those buyers get
  their own invoice and journal line.
- **Invoices.** By default, one invoice per identified customer plus **one
  summary invoice** to "Anonymous x402 customers" with a schedule of every
  wallet. (`--invoice-mode per-buyer` issues one per wallet instead.) Invoices
  are denominated in USDC, the currency charged, with the USD equivalent shown.
  Numbering is `CTR-YYYYMM-<seller>-<buyer|ANON>`, dated month-end, marked PAID,
  with every payment's transaction hash as settlement evidence. Without the
  seller's legal details (`--issuer issuer.json`) an invoice is a
  *payment-backed record*. A summary invoice to anonymous wallets is never
  presented as a tax invoice to a named customer. **[REVIEW]** Is a monthly
  summary invoice acceptable evidence of revenue for anonymous sales in the
  entity's jurisdiction? How is place of supply determined for VAT/GST when the
  customer cannot be identified?
- **Buyers are never named by Counterra.** Only names the seller supplies
  appear.

## 4. Buyer packs (spend)

- **Entry (identified seller):** Dr expense account by category (e.g.
  `6410 Data & Research`, `6420 AI & Compute`) / Cr `1085 Digital Assets
  (USDC)`.
- **Unidentified sellers go to suspense (decided).** A payment to a seller
  Counterra cannot identify is posted to `1990 x402 Suspense (unidentified
  counterparty)` (Dr 1990 / Cr 1085) and listed in `suspense_ledger.csv` with
  its likely spend type, inferred from the payment pattern. It is cleared to
  the right expense account once the seller is identified. Open suspense items
  are listed as exceptions: they should be cleared before the month is closed.
  **[REVIEW]** At year-end, should uncleared suspense be expensed (to
  uncategorised spend) rather than carried?
- **Invoices.** *Reconstructed purchase invoices*, one per seller per month,
  built from what was actually paid. They are evidence of payment, not a
  seller-issued invoice.
- **VAT (open).** A reconstructed invoice is expected **not** to support
  input-VAT recovery: EU and UK rules require a valid invoice issued by the
  supplier with its VAT number. The VAT route is seller-issued invoices
  (sellers using `--issuer` and `--customers`). **[REVIEW]** (a) Confirm. (b)
  Could reconstructed invoices work as *self-billed* invoices, issued by the
  customer under a self-billing agreement with the supplier, in the relevant
  jurisdictions? (c) For cross-border B2B services under reverse charge, what
  evidence does the buyer need?
- **Disposal of USDC.** Each payment is a disposal of USDC. **[REVIEW]** With
  daily-rate translation, a disposal gain or loss arises when the rate on the
  disposal day differs from the rate at acquisition. Counterra does not see
  acquisition cost (USDC bought elsewhere). Is the disposal count enough, or is
  lot tracking required?
- **Batch-channel deposits.** USDC moved into an x402 batch-settlement channel
  is posted to `1086 x402 Channel Escrow` (Dr 1086 / Cr 1085). It becomes spend
  only when the seller claims it. **[REVIEW]** Confirm prepaid-asset treatment.
- **Suspected address poisoning.** Payments to a wallet that imitates an
  established counterparty are held in `7910 Suspected Misdirected Payments`,
  not expense and not suspense, until confirmed. **[REVIEW]** Treatment once
  confirmed as theft.

## 5. Exceptions

Listed for review, and never silently dropped: suspected poisoning (high);
unidentified sellers in suspense (medium); missing exchange rates (medium); self-payments, dust and round-trips (medium);
closed loops, where one buyer and one seller deal almost only with each other
(low: possible self-testing or one dominant customer); single payments of $40
or more (low); customer concentration of 80% or more from one buyer (info);
escrow deposits (info).

## 6. Tie-out (printed on every pack)

1. Statement total = sum of invoices.
2. Statement total = journal total.
3. Every payment appears on exactly one invoice.
4. Statement total = the exact USD total (at daily average rates) rounded once to cents.
5. The QuickBooks file, read back after writing, balances.
6. The Xero file, read back after writing, balances.
7. Both files total the statement.
8. Invoices total the USDC actually paid on-chain (6 decimals).

A pack whose tie-out fails is marked FAIL on its first page.

## Questions for the reviewing accountant (summary)

1. Daily average rate, or spot at settlement? Who books period-end USDC
   remeasurement?
2. Sub-cent rounding: disclose, or post to a rounding account?
3. UTC months, or local time?
4. Point-in-time revenue recognition for x402 requests?
5. A monthly summary invoice to "Anonymous x402 customers": acceptable
   evidence? Place of supply for VAT/GST?
6. Suspense for unidentified sellers: year-end treatment if never identified?
7. VAT: do reconstructed invoices fail input-VAT recovery? Is self-billing an
   option? What evidence does reverse charge need?
8. USDC disposals: is the disposal count enough without lot tracking?
9. Escrow deposits as a prepaid asset?
10. Confirmed address-poisoning losses: which account, and when?
