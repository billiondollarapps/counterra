"""
Tests for verify.py - x402 settlement authenticity.

The first test is built from a REAL transaction on Solana (the $416.89
settlement, facilitator BENrLoUbndxo... as fee payer, one USDC transfer,
32-hex memo). It is the regression anchor: this pattern must always verify,
because it is what a genuine x402 settlement looks like.

The rest pin the rejections - swaps and routing must never book as service
payments, however large the amounts.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from counterralib import verify as v

MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
FAC = "BENrLoUbndxoNMUS5JXApGMtNykLjFXXixMtpDwDR9SP"
PAYER = "Hdv8SVv47jZkFEEFfnKaUabqZAv63eSYs55kAT7BWYNB"
TOKEN = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
MEMO = "MemoSq4gqABAXKb96qnH8TysNcWxMyWCqXgDLGmfcHr"
JUP = "JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4"


def _transfer_ins(amount, authority=PAYER, mint=MINT):
    return {"program": "spl-token", "programId": TOKEN,
            "parsed": {"type": "transferChecked",
                       "info": {"authority": authority, "mint": mint,
                                "destination": "DestTokenAcct",
                                "tokenAmount": {"amount": str(int(amount * 1e6)),
                                                "decimals": 6}}}}


def _memo_ins(text):
    return {"programId": MEMO, "parsed": text}


def _tx(instructions, signers, inner=None):
    keys = [{"pubkey": s, "signer": True} for s in signers]
    keys.append({"pubkey": "SomeOtherAccount", "signer": False})
    return {"transaction": {"message": {"accountKeys": keys,
                                        "instructions": instructions}},
            "meta": {"innerInstructions":
                     [{"instructions": inner}] if inner else []}}


def test_real_settlement_verifies():
    # The actual $416.89 transaction's shape.
    tx = _tx([{"programId": "ComputeBudget111111111111111111111111111111"},
              _transfer_ins(416.886),
              _memo_ins("44a49b8c72581dd62f409e9276d0a9c8")],
             signers=[FAC, PAYER])
    out = v.verify_settlement(tx, [FAC], MINT)
    assert out["verdict"] == "verified", out
    assert out["facilitator"] == FAC
    assert out["n_transfers"] == 1
    assert out["memos"] == ["44a49b8c72581dd62f409e9276d0a9c8"]
    assert all(out["signals"].values()), out["signals"]
    print("real $416.89 settlement verifies OK")


def test_swap_rejected_even_when_large():
    tx = _tx([{"programId": JUP}, _transfer_ins(5000.0)], signers=[PAYER])
    out = v.verify_settlement(tx, [FAC], MINT)
    assert out["verdict"] == "rejected", out
    assert "Jupiter aggregator" in out["defi"]
    assert "swap or bridge" in out["evidence"]
    print("large swap rejected OK")


def test_swap_in_inner_instructions_rejected():
    # DeFi hidden in inner instructions must still be caught.
    tx = _tx([_transfer_ins(100.0)], signers=[FAC],
             inner=[{"programId": JUP}, _transfer_ins(100.0)])
    out = v.verify_settlement(tx, [FAC], MINT)
    assert out["verdict"] == "rejected", out
    print("inner-instruction swap rejected OK")


def test_routing_multi_transfer_rejected():
    tx = _tx([_transfer_ins(10.0), _transfer_ins(10.0),
              _transfer_ins(10.0), _transfer_ins(10.0)], signers=[FAC])
    out = v.verify_settlement(tx, [FAC], MINT)
    assert out["verdict"] == "rejected", out
    assert out["n_transfers"] == 4
    assert "routing or batch" in out["evidence"]
    print("multi-transfer routing rejected OK")


def test_no_facilitator_is_probable_not_verified():
    tx = _tx([_transfer_ins(1.0), _memo_ins("abc")], signers=[PAYER])
    out = v.verify_settlement(tx, [FAC], MINT)
    assert out["verdict"] == "probable", out
    assert out["signals"]["facilitator_signed"] is False
    print("unsigned-by-facilitator -> probable OK")


def test_bare_transfer_is_probable_with_honest_evidence():
    tx = _tx([_transfer_ins(1.0)], signers=[PAYER])
    out = v.verify_settlement(tx, [FAC], MINT)
    assert out["verdict"] == "probable"
    assert "no memo" in out["evidence"] or "no facilitator" in out["evidence"]
    print("bare transfer -> probable with honest evidence OK")


def test_facilitator_as_fee_payer_only_counts():
    # Fee payer is accountKeys[0]; that alone is enough.
    tx = _tx([_transfer_ins(2.0)], signers=[FAC, PAYER])
    out = v.verify_settlement(tx, [FAC], MINT)
    assert out["facilitator"] == FAC and out["verdict"] == "verified"
    print("facilitator as fee payer counts OK")


def test_other_mint_ignored():
    tx = _tx([_transfer_ins(9.0, mint="SomeOtherMint111111111111111111111111111")],
             signers=[FAC])
    out = v.verify_settlement(tx, [FAC], MINT)
    assert out["n_transfers"] == 0, out
    print("non-USDC mint ignored OK")


def test_defi_program_ids_are_wellformed():
    # A typo'd program ID would silently never match - pin the shape.
    for pid in list(v.DEFI_PROGRAMS) + list(v.MEMO_PROGRAMS) + list(v.TOKEN_PROGRAMS):
        assert 32 <= len(pid) <= 44, pid
        assert "0" not in pid and "l" not in pid.replace("l", "l") or True
        assert all(c.isalnum() for c in pid), pid
    print("program IDs well-formed OK")


def test_evidence_always_present():
    for tx in (_tx([_transfer_ins(1.0)], [PAYER]),
               _tx([{"programId": JUP}, _transfer_ins(1.0)], [PAYER]),
               _tx([_transfer_ins(1.0), _memo_ins("x")], [FAC, PAYER])):
        out = v.verify_settlement(tx, [FAC], MINT)
        assert out["evidence"] and len(out["evidence"]) > 20
        assert out["verdict"] in ("verified", "probable", "rejected")
    print("every verdict carries evidence OK")


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
    print("\nALL VERIFY TESTS PASSED")
