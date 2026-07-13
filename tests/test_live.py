"""
Offline test for the live Base adapter.

Feeds the adapter fake HTTP responses shaped exactly like Etherscan V2
API payloads and verifies the decode path end to end - so the first
live run with a real API key holds no surprises.

Run:  python3 tests/test_live.py
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import yaml
from scribelib.live import BaseChainAdapter, TRANSFER_TOPIC

USDC = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
FAC = "0x9c09faa49c4235a09677159ff14f17498ac48738"
PAYER = "0x00000000000000000000000011110000000000000000000000000000000aa1"[-40:]
PAYEE = "0x000000000000000000000000a11ce000000000000000000000000000000001"[-40:]


class FakeResp:
    def __init__(self, payload): self.payload = payload
    def raise_for_status(self): pass
    def json(self): return self.payload


class FakeSession:
    """Returns canned Etherscan V2 responses keyed by module/action."""
    def get(self, url, params=None, timeout=None):
        a = (params or {}).get("action")
        if a == "txlist":
            return FakeResp({"status": "1", "result": [
                {"hash": "0xdeadbeef01", "timeStamp": "1751800000",
                 "to": USDC, "isError": "0"},
                {"hash": "0xdeadbeef02", "timeStamp": "1751803600",
                 "to": USDC, "isError": "0"},
                {"hash": "0xnotusdc003", "timeStamp": "1751807200",
                 "to": "0x0000000000000000000000000000000000000001", "isError": "0"},
            ]})
        if a == "eth_getTransactionReceipt":
            tx = (params or {}).get("txhash")
            amount_hex = hex(2500) if tx == "0xdeadbeef01" else hex(870000)  # 0.0025 / 0.87 USDC
            return FakeResp({"result": {"logs": [{
                "address": USDC,
                "topics": [TRANSFER_TOPIC,
                           "0x" + PAYER.rjust(64, "0"),
                           "0x" + PAYEE.rjust(64, "0")],
                "data": amount_hex,
            }]}})
        if a == "tokentx":
            return FakeResp({"status": "1", "result": [
                {"hash": "0xaaa1", "timeStamp": "1751800000", "from": "0x" + PAYER,
                 "to": "0x" + PAYEE, "value": "12500"},
                {"hash": "0xaaa2", "timeStamp": "1751803600", "from": "0x" + PAYEE,
                 "to": "0x" + PAYER, "value": "999999"},  # incoming - must be skipped
            ]})
        raise AssertionError(f"unexpected action {a}")


def main():
    cfg = yaml.safe_load(open(os.path.join(os.path.dirname(__file__), "..", "config.yaml")))
    ad = BaseChainAdapter(cfg, api_key="TESTKEY", session=FakeSession(), throttle=0)

    # facilitator sweep: 2 USDC txs decoded, non-USDC tx skipped (x2 facilitators in config)
    events = ad.fetch(limit=10)
    assert len(events) == 4, f"expected 4 events (2 per facilitator), got {len(events)}"
    amounts = sorted({round(e.amount_usdc, 4) for e in events})
    assert amounts == [0.0025, 0.87], amounts
    assert all(e.payer_wallet == "0x" + PAYER for e in events)
    assert all(e.payee_wallet == "0x" + PAYEE for e in events)
    assert all(e.protocol == "x402" for e in events)

    # wallet mode: only outgoing transfers kept
    wev = ad.fetch_wallet("0x" + PAYER)
    assert len(wev) == 1 and round(wev[0].amount_usdc, 4) == 0.0125, wev

    print("ALL TESTS PASSED")
    print(f"  facilitator sweep decoded {len(events)} PaymentEvents "
          f"(amounts: {amounts})")
    print(f"  wallet mode kept {len(wev)} outgoing transfer, skipped incoming")


if __name__ == "__main__":
    main()
