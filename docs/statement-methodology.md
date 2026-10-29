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

- **Currency.** Amounts are USDC, recorded at **1 USDC = 1 USD**. No exchange
  rate is applied. **[REVIEW]** Is par valuation acceptable for USDC in the
  reporting entity's jurisdiction, or must a daily fair-value rate be applied?
- **Precision.** USDC has 6 decimals. Every payment is kept exact to 6 decimal
  places. Rounding to cents happens **once**, on the period total, and the cents
  are then allocated across counterparties with the largest-remainder method.
  Per-counterparty lines therefore always sum exactly to the rounded total, and
  the difference from the exact total (under ½ cent) is stated on the tie-out.
  Rounding is half-up. **[REVIEW]** Is a disclosed sub-cent rounding difference
  acceptable, or should it be posted to a rounding account?
- **Timing.** A payment is recognised on its on-chain settlement timestamp
  (UTC). x402 settles at or immediately after the request is served, so service
  date and payment date coincide. Months follow UTC. **[REVIEW]** Should month
  boundaries follow the entity's local time zone?
- **Fees.** In x402 the facilitator pays network gas, so the buyer pays exactly
  the listed price and the seller receives it in full. There is no fee line.

## 3. Seller packs (revenue)

- **Entry (one per buyer per month):** Dr `1085 Digital Assets (USDC)` /
  Cr `4100 x402 Revenue`.
- **Revenue recognition.** Each paid request is a completed sale: the service
  is delivered in the same HTTP exchange that triggers payment, so there is no
  deferred revenue. **[REVIEW]** Confirm point-in-time recognition, especially
  for sellers whose single request starts a longer job.
- **Invoices.** One invoice per buyer wallet per month, numbered
  `CTR-YYYYMM-<seller code>-<buyer code>`, dated the last day of the month,
  marked PAID, with line items grouped by unit price and every payment's
  transaction hash as settlement evidence. Without the seller's legal name,
  address and tax ID (`--issuer issuer.json`) the document is a
  *payment-backed record*, not a tax invoice. **[REVIEW]**
  1. Is a monthly summary invoice per counterparty acceptable, or is one
     invoice per request required?
  2. The buyer is identified only by a wallet address. Can an invoice be issued
     to an unidentified customer, and does this change VAT/GST treatment
     (B2B vs B2C, place of supply)?
  3. For sellers with many tiny buyers (one demo seller has 230 buyers paying
     $18.84 in total), would one summary invoice for all anonymous buyers be
     the more sensible document?
- **Buyers are never named.** Counterra does not attach identities to buyer
  wallets.

## 4. Buyer packs (spend)

- **Entry (one per seller per month):** Dr expense account by category (e.g.
  `6410 Data & Research`, `6420 AI & Compute`; unidentified sellers go to
  `6480–6483` by payment pattern, or `6490 Uncategorized`) / Cr `1085 Digital
  Assets (USDC)`.
- **Invoices.** *Reconstructed purchase invoices*, one per seller per month,
  from what was actually paid. They are evidence of payment, not a seller-issued
  invoice. **[REVIEW]** Is this enough to support the expense, and does it
  support input-VAT recovery? (Expectation: no. A seller-issued tax invoice is
  needed for VAT.)
- **Disposal of USDC.** Each payment is a disposal of USDC. At par valuation
  the gain or loss is zero, and the count of disposals is reported. **[REVIEW]**
  Confirm that no gain or loss arises (cost basis of USDC at 1.00), and whether
  the disposal count must be reported.
- **Batch-channel deposits.** USDC moved into an x402 batch-settlement channel
  is posted to `1086 x402 Channel Escrow` (Dr 1086 / Cr 1085). It becomes spend
  only when the seller claims it. **[REVIEW]** Confirm prepaid-asset treatment.
- **Suspected address poisoning.** Payments to a wallet that imitates an
  established counterparty are held in `7910 Suspected Misdirected Payments`
  and not booked as expense until confirmed. **[REVIEW]** Treatment once
  confirmed as theft (loss account, timing).

## 5. Exceptions

Listed for review, and never silently dropped: suspected poisoning (high);
unidentified sellers (medium); self-payments, dust and round-trips (medium);
closed loops, where one buyer and one seller deal almost only with each other
(low: possible self-testing or one dominant customer); single payments of $40
or more (low); customer concentration of 80% or more from one buyer (info);
escrow deposits (info).

## 6. Tie-out (printed on every pack)

1. Statement total = sum of invoices.
2. Statement total = journal total.
3. Every payment appears on exactly one invoice.
4. Statement total = the exact payments total rounded once to cents.
5. The QuickBooks file, read back after writing, balances.
6. The Xero file, read back after writing, balances.
7. Both files total the statement.

A pack whose tie-out fails is marked FAIL on its first page.

## Questions for the reviewing accountant (summary)

1. Par valuation of USDC, or a fair-value rate?
2. Sub-cent rounding: disclose, or post to a rounding account?
3. UTC months, or local time?
4. Point-in-time revenue recognition for x402 requests?
5. Monthly summary invoices per counterparty: acceptable? To an anonymous
   wallet?
6. Reconstructed purchase invoices: enough for expense support? For VAT?
7. Zero gain or loss on USDC disposals at par; is the disposal count reportable?
8. Escrow deposits as a prepaid asset?
9. Confirmed address-poisoning losses: which account, and when?
