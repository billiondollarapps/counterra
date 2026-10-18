"""
Offline tests for the Base JSON-RPC collector (counterralib/base_rpc.py).
Run:  python3 tests/test_base_rpc.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import yaml
from counterralib import base_rpc as BR
from counterralib.live import TRANSFER_TOPIC

USDC = BR.USDC_BASE
FAC = "0x2a89407a98a0732b7fd578c4e156b7166540eb5a"
STRANGER = "0x9999999999999999999999999999999999999999"
AGENT = "0x1111000000000000000000000000000000000aa1"
SELLER = "0xa11ce00000000000000000000000000000000001"
PROXY = BR.PERMIT2_PROXIES[0]
HEAD = 1000


def t(a):
    return "0x" + a[2:].rjust(64, "0")


def transfer(frm, to, units):
    return {"address": USDC, "topics": [TRANSFER_TOPIC, t(frm), t(to)], "data": hex(units)}


RECEIPTS = {
    "0xaaa": {"status": "0x1", "from": FAC, "to": USDC, "blockNumber": hex(990),
              "logs": [transfer(AGENT, SELLER, 10000)]},                   # x402 exact
    "0xbbb": {"status": "0x1", "from": STRANGER, "to": USDC, "blockNumber": hex(991),
              "logs": [transfer(AGENT, SELLER, 777)]},                     # 3009, not x402
    "0xccc": {"status": "0x1", "from": STRANGER, "to": PROXY, "blockNumber": hex(995),
              "logs": [transfer(AGENT, SELLER, 2500000)]},                 # permit2 proxy
    "0xddd": {"status": "0x0", "from": FAC, "to": USDC, "blockNumber": hex(996),
              "logs": [transfer(AGENT, SELLER, 5)]},                       # reverted
}


class Resp:
    status_code = 200

    def __init__(self, payload):
        self.payload = payload

    def json(self):
        return self.payload


class FakeRPC:
    def __init__(self, batch=True, max_span=None):
        self.batch, self.max_span, self.calls = batch, max_span, []

    def _one(self, req):
        m, p = req["method"], req["params"]
        self.calls.append(m)
        if m == "eth_blockNumber":
            return {"id": req["id"], "result": hex(HEAD)}
        if m == "eth_getBlockByNumber":
            return {"id": req["id"], "result": {"timestamp": hex(1_760_000_000)}}
        if m == "eth_getLogs":
            f = p[0]
            a, b = int(f["fromBlock"], 16), int(f["toBlock"], 16)
            if self.max_span and b - a + 1 > self.max_span:
                return {"id": req["id"], "error": {"message": "block range too large"}}
            out = []
            if f["address"] == USDC and f.get("topics") == [[BR.AUTHORIZATION_USED]][0]:
                for h, blk in (("0xaaa", 990), ("0xbbb", 991), ("0xddd", 996)):
                    if a <= blk <= b:
                        out.append({"transactionHash": h})
            if f["address"] == BR.PERMIT2_PROXIES and a <= 995 <= b:
                out.append({"transactionHash": "0xccc"})
            return {"id": req["id"], "result": out}
        if m == "eth_getTransactionReceipt":
            return {"id": req["id"], "result": RECEIPTS.get(p[0])}
        raise AssertionError(m)

    def post(self, url, json=None, timeout=None):
        if isinstance(json, list):
            if not self.batch:
                return Resp({"error": "batch unsupported"})
            return Resp([self._one(r) for r in json])
        return Resp(self._one(json))


def run(session, state):
    cfg = yaml.safe_load(open(os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), "config.yaml")))
    ad = BR.BaseRpcAdapter(cfg, session=session, throttle=0, quiet=True,
                           rpc_url="https://rpc.test/x", chunk=50, state=state)
    assert ad.use_census({"wallets": [{"chain": "base", "address": FAC, "name": "Coinbase",
                                       "facilitator": "coinbase", "deprecated": False}]})
    return ad, ad.fetch(limit=100, budget_seconds=60, max_blocks=200)


def main():
    state = {}
    ad, evs = run(FakeRPC(), state)
    got = sorted((e.tx_hash, e.memo, round(e.amount_usdc, 4), e.facilitator) for e in evs)
    assert got == [("0xaaa", "x402:exact", 0.01, "Coinbase"),
                   ("0xccc", "x402:permit2", 2.5, "")], got
    assert ad.diag["not_facilitator"] == 1, "3009 from a non-facilitator is not x402"
    assert evs[0].ts.timestamp() == 1_760_000_000 - (HEAD - 990) * 2
    assert state[BR.STATE_KEY]["next_block"] == HEAD + 1
    assert "eth_getTransactionReceipt" in ad.session.calls

    # second run resumes at the watermark: nothing new, nothing re-read
    ad2, evs2 = run(FakeRPC(), state)
    assert evs2 == [] and ad2.diag["blocks"] == 0, ad2.diag

    # no batch support + narrow node limit: still correct
    ad3, evs3 = run(FakeRPC(batch=False, max_span=20), {})
    assert sorted(e.tx_hash for e in evs3) == ["0xaaa", "0xccc"]
    assert ad3.batch_ok is False

    # far behind: scans newest max_blocks, records the skipped range honestly
    st = {BR.STATE_KEY: {"next_block": 100}}
    ad4, _ = run(FakeRPC(), st)
    assert st[BR.STATE_KEY]["skipped_ranges"] == [[100, HEAD - 200]], st

    print("ALL BASE-RPC TESTS PASSED")
    print("  exact + permit2 decoded; non-facilitator EIP-3009 and reverted txs dropped")
    print("  block watermark resumes; batch fallback; node range limits; gaps recorded")


if __name__ == "__main__":
    main()
