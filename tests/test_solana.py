"""
Offline tests for the Solana collector.
Canned JSON-RPC responses shaped like mainnet: verifies signature
listing, transferChecked decoding, plain-transfer mint filtering via
token balances, token-account -> owner resolution, and wallet mode.
Run: python3 tests/test_solana.py
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import yaml
from counterralib.solana import SolanaChainAdapter

USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
OTHER_MINT = "So11111111111111111111111111111111111111112"
FAC = "92QcYJZpkwYyacR3G69QNR2JfjadQxd16cp5d7x5GEzU"
PAYER = "AgentPayerWallet11111111111111111111111111"
SELLER = "SellerOwnerWallet1111111111111111111111111"
PAYER_ATA, SELLER_ATA = "PayerTokenAcct111", "SellerTokenAcct11"

def make_tx(sig, transfers):
    keys = [FAC, PAYER_ATA, SELLER_ATA, PAYER, SELLER]
    balances = [
        {"accountIndex": 1, "mint": USDC, "owner": PAYER,
         "uiTokenAmount": {"amount": "1000000", "decimals": 6}},
        {"accountIndex": 2, "mint": USDC, "owner": SELLER,
         "uiTokenAmount": {"amount": "0", "decimals": 6}},
    ]
    return {
        "transaction": {"signatures": [sig],
                        "message": {"accountKeys": [{"pubkey": k} for k in keys],
                                    "instructions": transfers}},
        "meta": {"err": None, "innerInstructions": [],
                 "preTokenBalances": balances, "postTokenBalances": balances},
    }

TXS = {
    "sigA": make_tx("sigA", [{
        "program": "spl-token",
        "parsed": {"type": "transferChecked", "info": {
            "authority": PAYER, "source": PAYER_ATA, "destination": SELLER_ATA,
            "mint": USDC, "tokenAmount": {"amount": "150000", "decimals": 6}}}}]),
    "sigB": make_tx("sigB", [{      # plain transfer, mint via balances map
        "program": "spl-token",
        "parsed": {"type": "transfer", "info": {
            "authority": PAYER, "source": PAYER_ATA,
            "destination": SELLER_ATA, "amount": "50000"}}}]),
    "sigC": make_tx("sigC", [{      # wrong mint -> excluded
        "program": "spl-token",
        "parsed": {"type": "transferChecked", "info": {
            "authority": PAYER, "source": "x", "destination": "y",
            "mint": OTHER_MINT, "tokenAmount": {"amount": "999999", "decimals": 9}}}}]),
}

class FakeResp:
    def __init__(self, payload): self.payload = payload
    def raise_for_status(self): pass
    def json(self): return self.payload

class FakeSession:
    def post(self, url, json=None, timeout=None):
        m, p = json["method"], json["params"]
        if m == "getSignaturesForAddress":
            return FakeResp({"result": [
                {"signature": "sigA", "blockTime": 1752800000, "err": None},
                {"signature": "sigB", "blockTime": 1752800100, "err": None},
                {"signature": "sigC", "blockTime": 1752800200, "err": None},
                {"signature": "sigF", "blockTime": 1752800300, "err": "failed"},
            ]})
        if m == "getTransaction":
            return FakeResp({"result": TXS.get(p[0])})
        raise AssertionError("unexpected method " + m)

def main():
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    cfg = yaml.safe_load(open(os.path.join(here, "config.yaml")))
    cfg["solana"]["facilitators"] = [FAC]      # one facilitator for determinism
    ad = SolanaChainAdapter(cfg, session=FakeSession(), throttle=0, quiet=True)

    events = ad.fetch(limit=10)
    assert len(events) == 2, f"expected 2 USDC events (wrong-mint excluded), got {len(events)}"
    amounts = sorted(round(e.amount_usdc, 4) for e in events)
    assert amounts == [0.05, 0.15], amounts
    for e in events:
        assert e.payer_wallet == PAYER, "authority must resolve as payer"
        assert e.payee_wallet == SELLER, "token account must resolve to owner"
        assert e.chain == "solana"

    wev = ad.fetch_wallet(PAYER)
    assert len(wev) == 2 and all(e.payer_wallet == PAYER for e in wev)

    print("ALL SOLANA TESTS PASSED")
    print(f"  decoded {len(events)} events; transferChecked + plain transfer both handled")
    print("  wrong-mint excluded; failed tx skipped; passbook->owner resolution verified")

if __name__ == "__main__":
    main()
