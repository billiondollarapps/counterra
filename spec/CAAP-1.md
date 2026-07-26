# CAAP-1: Counterra Agent-Accounting Profile, v1 (Draft)

**Status:** Draft · **Version:** 1.0.0-draft · **Authors:** Counterra (billiondollarapps/counterra) · **License:** Apache-2.0

A normative, reproducible mapping from an x402 settlement (optionally with an
x402-receipts delivery receipt) to a double-entry accounting record.

## Why this exists

The x402 ecosystem is standardizing *payments* (the x402 protocol) and
*delivery receipts* (x402-receipts, Foundation issue #2833). Both stop at the
point of "a verified payment happened and this was delivered." Neither defines
how that becomes **books** — a journal entry an accountant, auditor, or tax
authority can consume.

That mapping is not obvious and it is not neutral: atomic-unit amounts must be
scaled, failed deliveries must not be booked as clean expense, partial
deliveries have undefined revenue recognition, and disposal dates matter for
tax. Today every accounting consumer solves these ad hoc, which means two tools
booking the same settlement can disagree. CAAP-1 fixes the mapping so that
**any conformant implementation produces the same journal entry from the same
inputs.**

This document is normative. The companion golden vectors
(`spec/caap1_vectors.json`) are the conformance suite: an implementation is
"CAAP-1 conformant" if it reproduces every expected output from the given
inputs.

The key words MUST, MUST NOT, SHOULD, and MAY are used per RFC 2119.

## Scope

CAAP-1 covers the transformation:

```
x402 settlement (required) + x402-receipts receipt (optional)
        ──►  one balanced double-entry journal record
```

It does **not** cover: signature/anchor verification (that is x402-receipts'
`verifyReceiptFull`), seller identity resolution (that is a registry concern),
or tax filing. CAAP-1 assumes inputs a consumer has already decided to trust.

## 1. Inputs

### 1.1 Settlement (required)
A settlement MUST provide: `chain`, `tx_hash`, `payer` (agent wallet),
`payee` (seller wallet), `amount` (see §2), and a timestamp.

### 1.2 Receipt (optional)
If present, a receipt MUST conform to `x402-receipts/v0` (v0.3 or later). CAAP-1
reads: `payment.asset`, `payment.amount`, `request.method`,
`response.status`, `response.latency_ms`, `goods.kind`, `goods.description`,
`delivery.status`, `response.body_sha256`, and
`request.payment_requirements_sha256`.

## 2. Amount normalization (NORMATIVE)

x402 and x402-receipts express `amount` in **atomic units** as a string
(e.g. USDC `"1000000"` = 1.000000). A conformant implementation MUST convert to
a decimal value by dividing by 10^decimals where:

- If the receipt/settlement supplies an explicit `decimals`, that value MUST be used.
- Else, decimals are resolved by asset symbol: USDC=6, USDT=6, DAI=18, WETH=18,
  ETH=18, USD=2.
- Else, decimals default to **6** (x402 settles overwhelmingly in 6-decimal USDC).

Implementations MUST NOT silently book an atomic-unit integer as a dollar value.
A value that cannot be parsed to an integer MUST raise, not book zero.

> **Standards note (fed back to x402-receipts #2833):** the absence of an
> explicit `decimals`/`amount_display` field forces this resolution onto every
> consumer. CAAP-1 defines the fallback so consumers agree, but an explicit
> field upstream would remove the ambiguity entirely.

## 3. Double-entry mapping (NORMATIVE)

Each settlement produces exactly **two** lines that MUST balance
(debit total == credit total):

| Line   | Account                              | Amount |
|--------|--------------------------------------|--------|
| Debit  | expense account for the seller's category | value  |
| Credit | Digital-asset (USDC) account         | value  |

Rationale: agent spend is the acquisition of a service (expense, debit) paid by
disposing of a digital asset (credit). The credit line is a digital-asset
disposal and each settlement MUST retain its disposal count for tax treatment.

### 3.1 Expense account selection
The debit account is chosen by the seller's **category**. Category is resolved
in this order:
1. Registry mapping for the payee wallet, if the wallet is known.
2. Else, from receipt `goods.kind`: `api-response`→AI/Compute, `dataset`/`file`→
   Data/Research, `text`→AI/Compute.
3. Else, **Uncategorized** → a single uncategorized-spend account.

An implementation MUST NOT collapse distinct known categories into one account.

## 4. Bookability and exceptions (NORMATIVE)

Not every trusted settlement is clean expense. An implementation MUST classify a
record as **bookable** only if BOTH:
- delivery status is `delivered` (or no receipt is present — settlement-only
  records are assumed delivered), AND
- if a receipt is present, `response.status` is in [200,300).

Records failing either test MUST be routed to an **exception queue**, not booked
as clean expense, with a reason:

| Condition                          | Exception reason (normative meaning)                        |
|------------------------------------|-------------------------------------------------------------|
| `delivery.status == "failed"`      | payment made, nothing delivered — not an expense            |
| `delivery.status == "partial"`     | revenue recognition undefined (no delivered_fraction in v0) |
| `response.status` outside [200,300)| payment made against an error response — review             |
| payee not in registry              | unmapped counterparty — needs classification                |

> **Standards note:** `delivery.status == "partial"` is unbookable under CAAP-1
> because v0 carries no magnitude. If x402-receipts adds
> `delivery.delivered_fraction`, CAAP-1 v1.1 will define proportional
> recognition.

## 5. Exception grouping (NORMATIVE)

When reporting exceptions, repeated settlements to the **same unmapped payee**
MUST be grouped into a single exception (one classification task), carrying
settlement count, distinct-payer count, total amount, and time window. Anomalies
(single payments above a materiality threshold) MUST remain per-settlement.

Rationale: 131 settlements to one unmapped wallet is one problem, not 131.

## 6. Audit binding (NORMATIVE)

Where a receipt is present, the journal record MUST retain: `tx_hash`,
`response.body_sha256`, and `request.payment_requirements_sha256`. These bind
the journal line to a re-verifiable receipt, so an auditor can confirm the entry
against the signed source document. A record without a receipt retains at least
`tx_hash`.

## 7. Disposal date (NORMATIVE)

The booking date is the **settlement date** (on-chain finality), which for tax
is the digital-asset disposal date. If only receipt timestamps are available,
`payment.settled_ts` MUST be preferred; absent that, `response.ts`; absent that,
`request.ts`.

> **Standards note:** receipts carry request/response timestamps but not the
> settlement time, so a receipt is not self-sufficient for booking without the
> chain. An optional `payment.settled_ts` upstream would close this.

## 8. Conformance

An implementation is **CAAP-1 conformant** if, for every vector in
`spec/caap1_vectors.json`, it produces a journal record whose normative fields
(debit_account, credit_account, amount_usd, bookable, exception_reason, date,
category) equal the vector's `expect`.

Counterra's `counterralib/receipts.py` and `counterralib/ledger.py` are the
reference implementation.

## Changelog
- **1.0.0-draft** — initial profile: amount normalization, double-entry mapping,
  bookability, exception grouping, audit binding, disposal date.
