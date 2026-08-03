"""
Consume x402-receipts (v0.3) as accounting source documents.

The x402-receipts project (StelarDigital, Foundation issue #2833) standardises a
signed delivery receipt for each x402 payment: the settlement plus a hash-bound
record of what was requested, what was delivered, and the delivery outcome.

Every participant in that thread is building the TRUST side — is the receipt
real, did settlement happen, is the seller sybil-ing. None is looking at what
the receipt is FOR once trusted: it is the source document an accounting layer
turns into a journal entry. A settlement alone is a bank-statement line ("money
left this wallet"); a receipt is the invoice ("...for GET /signal, delivered,
340ms"). Counterra already produces the former; this module consumes the receipt
to produce the latter.

This does NOT verify signatures or anchors — x402-receipts' own verifyReceipt /
verifyReceiptFull is the authority for "is this receipt valid". This module
assumes a receipt Counterra has decided to trust and asks the next question:
what journal entry does it produce, and does it reconcile against the settlement
we already decoded on-chain?

WHAT BUILDING THIS SURFACED (the accounting-angle feedback for #2833):
  1. `amount` is atomic-unit string with no `decimals` field. Booking requires
     dividing by the asset's decimals; the receipt makes a consumer resolve that
     out-of-band (USDC=6, but not every asset is). An explicit `decimals` — or a
     decimal `amount_display` — would remove an error-prone lookup from every
     accounting consumer.
  2. There is no currency/settlement-date beyond `payment` + response `ts`. For
     books, the disposal date of the asset is the settlement timestamp; receipts
     carry response.ts (delivery) and request.ts, but the on-chain settlement
     time lives only in the tx. Fine when the consumer also has the chain (we
     do), but a `settled_ts` would make the receipt self-sufficient for booking.
  3. `delivery.status = "partial"` has no amount/proportion, so revenue
     recognition on a partial delivery is undefined. An optional
     `delivery.delivered_fraction` (or amount) would let books recognise partial
     revenue instead of all-or-nothing.
  4. No category/SKU. `goods.kind` ("api-response" | "dataset" | ...) is the
     closest thing to an expense category; usable, but coarse. A free-text
     `goods.category` or resource-tag would map straight to a GL account.
"""

from __future__ import annotations

import hashlib
import json
from typing import Optional

# Known asset decimals for booking atomic-unit amounts. USDC is 6 on Base/Solana.
ASSET_DECIMALS = {
    "USDC": 6, "USDT": 6, "USD": 2, "DAI": 18, "WETH": 18, "ETH": 18,
}
DEFAULT_DECIMALS = 6  # x402 settles overwhelmingly in USDC


class ReceiptError(ValueError):
    pass


def _require(d, path):
    cur = d
    for key in path.split("."):
        if not isinstance(cur, dict) or key not in cur:
            raise ReceiptError(f"receipt missing required field: {path}")
        cur = cur[key]
    return cur


def _decimals_for(asset, decimals_hint=None, asset_address=None):
    if decimals_hint is not None:
        return int(decimals_hint)
    # v0.5.1 binds an optional asset_address (CAIP-19 / contract). Base USDC is
    # 6 decimals; recognise it explicitly so a contract-bound receipt resolves
    # without relying on the symbol string.
    if asset_address:
        aa = str(asset_address).lower()
        if aa.endswith("833589fcd6edb6e08f4c7c32d4f71b54bda02913"):  # Base USDC
            return 6
    if not asset:
        return DEFAULT_DECIMALS
    sym = asset.upper()
    # tolerate CAIP-19 / contract strings by falling back to the default
    return ASSET_DECIMALS.get(sym, DEFAULT_DECIMALS)


def amount_to_usd(atomic_amount, asset="USDC", decimals=None, asset_address=None):
    """
    Convert a receipt's atomic-unit amount string to a decimal value.

    Gap #1 in the docstring: the receipt gives "1000000" and expects the
    consumer to know USDC has 6 decimals. v0.5.1 adds an optional asset_address
    (contract binding) we use when present; otherwise we resolve by symbol, but
    still flag that the schema pushes this onto every accounting consumer.
    """
    d = _decimals_for(asset, decimals, asset_address)
    try:
        return int(str(atomic_amount)) / (10 ** d)
    except (ValueError, TypeError):
        raise ReceiptError(f"un-bookable amount: {atomic_amount!r}")


def receipt_to_journal(receipt, registry=None, expense_accounts=None,
                       digital_asset_account="1085 - Digital Assets (USDC)",
                       verified=None, failure_code=None):
    """
    Turn a trusted x402 receipt into an enriched journal entry.

    `verified`: the verdict from x402-receipts verifyReceiptFull, if the caller
    ran it. CAAP-1 does NOT recompute verification (that's the receipts spec's
    job) — but if the caller passes verified=False, the receipt MUST NOT be
    booked: it becomes an 'unverified-receipt' exception. Passing None means
    "caller asserts this receipt is already trusted" (the default, for callers
    that verify upstream before handing receipts to the books).

    Returns a dict with the double-entry lines plus the delivery context a
    settlement alone can't provide (what was bought, whether it was delivered,
    latency, and the hashes that bind the entry to a verifiable receipt).
    """
    scheme = receipt.get("scheme", "")
    if not str(scheme).startswith("x402-receipts/"):
        raise ReceiptError(f"not an x402 receipt: scheme={scheme!r}")

    pay = _require(receipt, "payment")
    req = _require(receipt, "request")
    resp = _require(receipt, "response")

    payee = str(pay["payee"]).lower()
    asset = pay.get("asset", "USDC")
    usd = amount_to_usd(pay["amount"], asset, asset_address=pay.get("asset_address"))

    reg = registry or {}
    entry_meta = reg.get(payee) or {}
    label = entry_meta.get("label") or pay["payee"]
    category = entry_meta.get("category") or _goods_category(receipt)

    exp = expense_accounts or {}
    debit_account = exp.get(category, exp.get("Uncategorized",
                                              "6490 - Uncategorized Agent Spend"))

    delivery = (receipt.get("delivery") or {}).get("status", "delivered")
    goods = receipt.get("goods") or {}
    resp_status = resp.get("status")

    # Bookability + exception routing.
    #
    # CAAP-1 v1.1 aligns its exception codes to the ReceiptFailure taxonomy
    # proposed by PatrickPi1312 (eucompliance.tools) in x402-foundation/x402#2833,
    # which routes on WHO MUST ACT rather than where the failure was detected:
    #
    #   settlement_missing  delivery claimed, no matching on-chain settlement  -> buyer / payment side
    #   delivery_failed     settled on-chain, seller says delivery failed      -> seller (refund case)
    #   receipt_invalid     signature / schema / issuer failed                 -> seller (reissue)
    #   receipt_tampered    content hash != signed digest (altered after issue) -> hard stop, manual review
    #   receipt_expired     past valid_until - routine, not a defect           -> seller (re-request)
    #
    # receipt_expired (added at PatrickPi1312's suggestion, #2833) is split out
    # from receipt_invalid on purpose: an expired receipt is routine seller-side
    # ops, so a books consumer may re-request rather than queue it for review —
    # a different queue behaviour than a structural defect. Same who-acts
    # principle: distinct action => distinct code.
    #
    # `failure_code`: if the caller ran verifyReceiptFull and it failed, they
    # pass the ReceiptFailure.code here and CAAP-1 routes on it directly.
    # `verified=False` without a code maps to receipt_invalid (generic).
    # A tampered receipt is NEVER booked and is flagged for manual review, not
    # just reissue — an altered document (e.g. a corrected VAT rate after
    # signing) is a fraud signal, which is exactly why Patrick's split matters
    # to the books: it must not land in the same bucket as an expired key.
    delivery_ok = (delivery == "delivered"
                   and isinstance(resp_status, int) and 200 <= resp_status < 300)

    exc_code = None
    if failure_code in ("settlement_missing", "delivery_failed",
                        "receipt_invalid", "receipt_tampered", "receipt_expired"):
        exc_code = failure_code
    elif verified is False:
        exc_code = "receipt_invalid"
    elif not delivery_ok:
        # no explicit verification verdict, but the receipt's own delivery
        # status / HTTP code says the service didn't deliver
        exc_code = "delivery_failed"

    bookable = (exc_code is None)
    exc_reason = _exception_reason_for_code(exc_code, delivery, resp_status)
    who_acts = _who_acts(exc_code)

    return {
        "date": (pay.get("settled_ts") or resp.get("ts") or req.get("ts", ""))[:10],
        "debit_account": debit_account,
        "credit_account": digital_asset_account,
        "amount_usd": round(usd, 6),
        "payee_wallet": pay["payee"],
        "provider": label,
        "category": category,
        # the enrichment a bare settlement can't give you:
        "delivery_status": delivery,
        "http_status": resp_status,
        "latency_ms": resp.get("latency_ms"),
        "goods_kind": goods.get("kind"),
        "goods_description": goods.get("description"),
        "resource_method": req.get("method"),
        # audit binding — lets the journal line be re-verified against the receipt:
        "tx_hash": pay.get("tx_hash"),
        "body_sha256": resp.get("body_sha256"),
        "payment_requirements_sha256": req.get("payment_requirements_sha256"),
        "bookable": bookable,
        "exception_code": exc_code,
        "exception_reason": None if bookable else exc_reason,
        "exception_actor": None if bookable else who_acts,
    }


def _goods_category(receipt):
    """Best available category from a receipt, given no dedicated field (gap #4)."""
    goods = receipt.get("goods") or {}
    kind = goods.get("kind")
    mapping = {
        "api-response": "AI & Compute Services",
        "dataset": "Data & Research Services",
        "file": "Data & Research Services",
        "text": "AI & Compute Services",
    }
    return mapping.get(kind, "Uncategorized")


def _exception_reason_for_code(code, delivery, http_status):
    """Human-readable reason for each ReceiptFailure code (or delivery detail)."""
    if code is None:
        return None
    if code == "settlement_missing":
        return ("Delivery claimed but no matching on-chain settlement - "
                "payment side must resolve")
    if code == "delivery_failed":
        if delivery == "partial":
            return ("Receipt reports delivery.status=partial - revenue "
                    "recognition undefined (no delivered_fraction); seller/refund")
        if isinstance(http_status, int) and not (200 <= http_status < 300):
            return (f"Settled but response.status={http_status} - service did not "
                    f"deliver a success response; seller/refund case")
        return ("Receipt reports delivery.status=failed - payment made, nothing "
                "delivered; seller/refund case")
    if code == "receipt_invalid":
        return ("Receipt failed verification (signature/schema/issuer) - not a "
                "trusted source document; seller must reissue")
    if code == "receipt_tampered":
        return ("Receipt content hash does not match the signed digest - altered "
                "after issuance; HARD STOP, manual review (fraud signal)")
    if code == "receipt_expired":
        return ("Receipt is past valid_until - routine seller-side expiry, not a "
                "defect; re-request rather than queue for review")
    return "Unbookable receipt - review"


def _who_acts(code):
    """Route each exception to who must act on it — Patrick's key insight."""
    return {
        "settlement_missing": "buyer",     # payment side
        "delivery_failed": "seller",       # refund case
        "receipt_invalid": "seller",       # reissue
        "receipt_expired": "seller",        # re-request (routine)
        "receipt_tampered": "manual",      # hard stop, human review
    }.get(code)


def _legacy_exception_reason(delivery, http_status):
    if delivery == "failed":
        return "Receipt reports delivery.status=failed - payment made, nothing delivered"
    if delivery == "partial":
        return ("Receipt reports delivery.status=partial - revenue recognition "
                "undefined (no delivered_fraction in v0.3)")
    if not (isinstance(http_status, int) and 200 <= http_status < 300):
        return f"Receipt response.status={http_status} - not a success response"
    return "Unbookable receipt - review"


def reconcile_with_settlement(receipt, payment_event):
    """
    Check a receipt against a settlement Counterra already decoded on-chain.

    The receipt is the seller's claim; the PaymentEvent is what we observed on
    Base/Solana. If they disagree, that is an exception with cryptographic
    grounds — exactly what the exception queue is for. Returns a list of
    mismatch strings (empty when they agree).
    """
    problems = []
    pay = receipt.get("payment", {})

    rtx = str(pay.get("tx_hash", "")).lower()
    etx = str(getattr(payment_event, "tx_hash", "")).lower()
    if rtx and etx and rtx != etx:
        problems.append(f"tx_hash mismatch: receipt {rtx[:14]} vs ledger {etx[:14]}")

    try:
        r_usd = amount_to_usd(pay.get("amount"), pay.get("asset", "USDC"), asset_address=pay.get("asset_address"))
        e_usd = float(getattr(payment_event, "amount_usdc", 0))
        if abs(r_usd - e_usd) > 1e-6:
            problems.append(f"amount mismatch: receipt ${r_usd:.6f} vs ledger ${e_usd:.6f}")
    except ReceiptError:
        problems.append("receipt amount un-parseable for reconciliation")

    r_payee = str(pay.get("payee", "")).lower()
    e_payee = str(getattr(payment_event, "payee_wallet", "")).lower()
    if r_payee and e_payee and r_payee != e_payee:
        problems.append(f"payee mismatch: receipt {r_payee[:14]} vs ledger {e_payee[:14]}")

    return problems
