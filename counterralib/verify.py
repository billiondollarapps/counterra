"""
Settlement authenticity verification for Solana.

The collector sweeps facilitator signature lists and decodes USDC transfers.
But `getSignaturesForAddress` returns every transaction that REFERENCES a
facilitator, not only those it settled - so the raw sweep can pick up swap
legs, bridge hops and ordinary transfers alongside genuine x402 payments.
Counterra publishes volume figures derived from this data, and a permissive
ingest filter would reproduce exactly the overstatement that wash-filtering
analyses have criticised elsewhere. So authenticity gets checked and, more
importantly, RECORDED.

Signals, strongest first:

  facilitator_signed   a configured facilitator is the fee payer or a signer.
                       The facilitator actually submitted this settlement
                       rather than merely appearing in it. Strongest signal.
  memo_present         an x402 payment identifier in the memo program.
  single_transfer      exactly one USDC transfer - a payment, not routing.
  no_defi              no DEX/AMM/bridge program invoked. A swap's USDC legs
                       are not service payments however large.

Verdicts: "verified" (facilitator-signed AND no DeFi AND not multi-hop),
"probable" (payment-shaped but no facilitator signature), "rejected" (DeFi
programs present, or routing-shaped). Rejected settlements should not be
booked as x402 spend.

Read-only: this module inspects, it never signs or sends anything.
"""

from __future__ import annotations

# DEX / AMM / bridge programs. USDC moving through these is a swap or bridge
# leg, never a service payment.
DEFI_PROGRAMS = {
    "JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4": "Jupiter aggregator",
    "JUP4Fb2cqiRUcaTHdrPC8h2gNsA2ETXiPDD33WcGuJB": "Jupiter v4",
    "675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8": "Raydium AMM",
    "CAMMCzo5YL8w4VFF8KVHrK22GGUsp5VTaW7grrKgrWqK": "Raydium CLMM",
    "whirLbMiicVdio4qvUfM5KAg6Ct8VwpYzGff3uctyCc": "Orca Whirlpool",
    "9WzDXwBbmkg8ZTbNMqUxvQRAyrZzDsGYdLVL9zYtAWWM": "Orca v1",
    "worm2ZoG2kUd4vFXhvjh93UUH596ayRfgQ2MgjNMTth": "Wormhole bridge",
    "PhoeNiXZ8ByJGLkxNfZRnkUfjvmuYqLR89jjFHGqdXY": "Phoenix DEX",
    "srmqPvymJeFKQ4zGQed1GFppgkRHL9kaELCbyksJtPX": "Serum DEX",
}

MEMO_PROGRAMS = ("MemoSq4gqABAXKb96qnH8TysNcWxMyWCqXgDLGmfcHr",
                 "Memo1UhkJRfHyvLMcVucJwxXeuD728EqVDDwQDxFMNo")

TOKEN_PROGRAMS = ("TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",
                  "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb")


def _account_keys(msg):
    return [k["pubkey"] if isinstance(k, dict) else k
            for k in msg.get("accountKeys", [])]


def _signers(msg):
    return [k["pubkey"] for k in msg.get("accountKeys", [])
            if isinstance(k, dict) and k.get("signer")]


def _program_of(ins, keys):
    p = ins.get("programId")
    if p:
        return p
    i = ins.get("programIdIndex")
    return keys[i] if i is not None and i < len(keys) else "?"


def all_instructions(tx):
    """Top-level plus inner instructions, flattened."""
    msg = (tx.get("transaction") or {}).get("message") or {}
    meta = tx.get("meta") or {}
    out = list(msg.get("instructions", []))
    for g in (meta.get("innerInstructions") or []):
        out.extend(g.get("instructions", []))
    return out


def usdc_transfers(tx, mint):
    """[(amount, authority)] for every USDC transfer instruction."""
    out = []
    for ins in all_instructions(tx):
        if ins.get("program") != "spl-token":
            continue
        pr = ins.get("parsed") or {}
        if pr.get("type") not in ("transfer", "transferChecked"):
            continue
        info = pr.get("info", {})
        if "tokenAmount" in info:
            ta = info["tokenAmount"]
            if info.get("mint") and info["mint"] != mint:
                continue
            amt = int(ta["amount"]) / 10 ** int(ta.get("decimals", 6))
        else:
            amt = int(info.get("amount", "0")) / 10 ** 6
        out.append((amt, info.get("authority") or
                    info.get("multisigAuthority") or "?"))
    return out


def memos(tx):
    out = []
    keys = _account_keys((tx.get("transaction") or {}).get("message") or {})
    for ins in all_instructions(tx):
        if _program_of(ins, keys) in MEMO_PROGRAMS:
            pr = ins.get("parsed")
            if isinstance(pr, str):
                out.append(pr)
            elif pr is not None:
                out.append(str(pr))
    return out


def defi_programs(tx):
    keys = _account_keys((tx.get("transaction") or {}).get("message") or {})
    found = []
    for ins in all_instructions(tx):
        p = _program_of(ins, keys)
        if p in DEFI_PROGRAMS and DEFI_PROGRAMS[p] not in found:
            found.append(DEFI_PROGRAMS[p])
    return found


def verify_settlement(tx, facilitators, mint):
    """
    Assess whether a transaction is a genuine x402 settlement.

    Returns {verdict, signals, evidence, defi, n_transfers, memos,
    fee_payer, facilitator}. Verdict is "verified", "probable" or
    "rejected". Evidence is a one-line human-readable justification, meant
    to be stored alongside the event so the judgement is auditable later.
    """
    facs = set(facilitators or [])
    msg = (tx.get("transaction") or {}).get("message") or {}
    keys = _account_keys(msg)
    signers = _signers(msg)
    fee_payer = keys[0] if keys else None

    fac_hit = None
    for cand in ([fee_payer] if fee_payer else []) + signers:
        if cand in facs:
            fac_hit = cand
            break

    defi = defi_programs(tx)
    xfers = usdc_transfers(tx, mint)
    ms = memos(tx)

    signals = {
        "facilitator_signed": bool(fac_hit),
        "memo_present": bool(ms),
        "single_transfer": len(xfers) == 1,
        "no_defi": not defi,
    }

    if defi:
        verdict = "rejected"
        evidence = ("DeFi program(s) invoked ({}) - USDC movement is a swap or "
                    "bridge leg, not a service payment".format(", ".join(defi)))
    elif len(xfers) > 2:
        verdict = "rejected"
        evidence = ("{} USDC transfers in one transaction - routing or batch "
                    "movement, not a single service payment".format(len(xfers)))
    elif fac_hit:
        verdict = "verified"
        evidence = ("facilitator {}... signed and paid fees for this "
                    "transaction; {} USDC transfer(s){}".format(
                        fac_hit[:12], len(xfers),
                        ", memo present" if ms else ", no memo"))
    elif ms and len(xfers) == 1:
        verdict = "probable"
        evidence = ("single USDC transfer with a memo, but no configured "
                    "facilitator signed it - payment-shaped, unconfirmed")
    else:
        verdict = "probable"
        evidence = ("plain USDC transfer, no facilitator signature and no "
                    "memo - could be an ordinary transfer near a facilitator")

    return {"verdict": verdict, "signals": signals, "evidence": evidence,
            "defi": defi, "n_transfers": len(xfers), "memos": ms,
            "fee_payer": fee_payer, "facilitator": fac_hit}
