# CAAP-1: Counterra Agent-Accounting Profile, v1 (Draft)

**Status:** Draft · **Version:** 1.0.0-draft · **Authors:** Counterra (billiondollarapps/counterra) · **License:** Apache-2.0

**Aligned to:** x402-receipts **v0.5.1** (StelarDigital/x402-receipts, commit `debc94f`). CAAP-1's envelope field names are pinned to v0.5.1 naming so the two specs stay diffable as both move (per x402-foundation/x402#2833). When x402-receipts advances, this header records the receipts version CAAP-1's reference consumer is verified against.

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

Not every settlement is clean expense. An implementation MUST classify a record
as **bookable** only if ALL hold:
- the caller's verification verdict is not a failure (see §4.1), AND
- delivery status is `delivered` (or no receipt is present — settlement-only
  records are assumed delivered), AND
- if a receipt is present, `response.status` is in [200,300).

Records failing any test MUST be routed to an **exception queue**, not booked as
clean expense.

### 4.1 Exception taxonomy (routes on who must act)

CAAP-1 exception codes align to the `ReceiptFailure` taxonomy proposed by
PatrickPi1312 (eucompliance.tools) in x402-foundation/x402#2833. The key
principle is that an exception routes on **who must act**, which is a different
axis from where the failure was detected:

| code                 | meaning                                              | actor  |
|----------------------|------------------------------------------------------|--------|
| `settlement_missing` | delivery claimed, no matching on-chain settlement    | buyer  |
| `delivery_failed`    | settled, but `delivery.status=failed`/partial or non-2xx | seller (refund) |
| `receipt_invalid`    | signature / schema / issuer verification failed      | seller (reissue) |
| `receipt_expired`    | past `valid_until` — routine expiry, not a defect    | seller (re-request) |
| `receipt_tampered`   | content hash ≠ signed digest (altered after issuance) | manual (hard stop) |

`receipt_expired` is kept distinct from `receipt_invalid` (per PatrickPi1312,
#2833): an expired receipt is routine seller-side ops, so a books consumer may
re-request it rather than queue it for review — a different queue behaviour than
a structural defect.

The distinction between `receipt_invalid` and `receipt_tampered` is load-bearing
for the books: a tampered receipt (e.g. a VAT rate edited after signing) is a
**fraud signal** that MUST hard-stop to manual review, while an invalid one (an
expired key, schema drift) is a seller-side operational problem. Collapsing both
into one code forces manual triage of routine issues — which is exactly what an
exception queue should avoid.

A caller that ran x402-receipts `verifyReceiptFull` SHOULD pass the resulting
`ReceiptFailure.code`; CAAP-1 routes on it directly. Absent an explicit code, a
failed verification verdict maps to `receipt_invalid`, and a receipt whose own
delivery status or HTTP code indicates non-delivery maps to `delivery_failed`.

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

### 6.1 External document binding (OPTIONAL)

A settlement may have a formal document sitting alongside the receipt — a tax
invoice, a credit note, a purchase order. A journal record MAY retain an
`external_documents` array binding the entry to such documents:

```json
"external_documents": [
  {"kind": "invoice", "hash": "sha256:9f2b…", "hash_alg": "sha256"}
]
```

- `hash` MUST be a content hash of the document's canonical bytes as issued.
  An implementation MUST NOT re-serialize or normalize the document before
  hashing; the hash binds the bytes an auditor will be handed.
- `hash_alg` MUST be present when the algorithm is not `sha256`.
- `kind` is a free-text label describing the document's role, not its format.

This binding is deliberately **format-agnostic and jurisdiction-agnostic**. The
same field holds a hash of a UBL or CII e-invoice issued under EN 16931, a US
PDF invoice, an Indian GST e-invoice, or any other instrument. CAAP-1
standardizes *that* the link exists and how it is expressed, so payment,
delivery receipt and formal document reconcile over one key — it does not
define what is on the other end of the link, and a conformant implementation
is not required to parse, validate, or understand the referenced document.

> **Standards note (per PatrickPi1312, billiondollarapps/counterra#8):** the
> motivating case is the EU e-invoicing rollout, where a growing share of
> booked agent payments will carry a formal e-invoice beside the x402 receipt.
> Validating that invoice against a jurisdiction's business rules (e.g. the
> EN 16931 Schematron rule set) is **consumer-side**, not spec-side: an
> implementer subject to those rules validates before booking and, on failure,
> routes the record to the exception queue under the existing taxonomy —
> typically `receipt_invalid` (seller must reissue) — carrying the
> jurisdiction's own finding code in the exception evidence. CAAP-1 does not
> define jurisdiction-specific validity, because a code that is dead for
> implementers outside one regulatory area would make the conformance suite
> partially untestable for them. §4.1's codes route on *who must act*, which
> is invariant across jurisdictions; what makes a document invalid is not.

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
- **1.0.1-draft** — §6.1: optional `external_documents` content-hash binding, so
  a formal invoice or other instrument reconciles with the settlement and
  receipt over one key. Format- and jurisdiction-agnostic by construction;
  jurisdiction-specific document validation is consumer-side, routed through the
  existing §4.1 taxonomy (per billiondollarapps/counterra#8).
