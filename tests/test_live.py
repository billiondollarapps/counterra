"""
Offline tests for the live adapter + facilitator refresh.
No network, no API key: canned responses shaped like the real APIs.
Run:  python3 tests/test_live.py
"""
import sys, os, json, tempfile
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import yaml
from scribelib.live import BaseChainAdapter, refresh_facilitators, TRANSFER_TOPIC

USDC = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
PAYER = "1111000000000000000000000000000000000aa1"
PAYEE = "a11ce00000000000000000000000000000000001"


class FakeResp:
    def __init__(self, payload, text=""):
        self.payload, self.text = payload, text
    def raise_for_status(self): pass
    def json(self): return self.payload


class FakeBlockscout:
    """Serves Blockscout v2 REST shapes (the default live path)."""
    def get(self, url, params=None, timeout=None):
        if "/addresses/" in url and "/transactions" in url:
            return FakeResp({"items": [
                {"hash": "0xaaa1", "timestamp": "2026-07-13T11:53:00Z",
                 "to": {"hash": USDC}, "status": "ok"},
                {"hash": "0xzzz9", "timestamp": "2026-07-13T11:54:00Z",
                 "to": {"hash": "0x0000000000000000000000000000000000000001"},
                 "status": "ok"},                       # non-USDC: skipped
            ], "next_page_params": None})
        if "/transactions/" in url and "/logs" in url:
            return FakeResp({"items": [{
                "address": {"hash": USDC},
                "topics": [TRANSFER_TOPIC,
                           "0x" + PAYER.rjust(64, "0"),
                           "0x" + PAYEE.rjust(64, "0")],
                "data": hex(10000),                     # 0.01 USDC
            }]})
        # legacy endpoint used by fetch_wallet
        if params and params.get("action") == "tokentx":
            return FakeResp({"status": "1", "result": [
                {"hash": "0xbbb1", "timeStamp": "1752407580",
                 "from": "0x" + PAYER, "to": "0x" + PAYEE, "value": "12500"},
                {"hash": "0xbbb2", "timeStamp": "1752407581",
                 "from": "0x" + PAYEE, "to": "0x" + PAYER, "value": "999"},
            ]})
        raise AssertionError(f"unexpected url {url} params {params}")


class FakeRegistry:
    def get(self, url, timeout=None):
        return FakeResp({}, text="""
          { address: '0x1111111111111111111111111111111111111111',
            dateOfFirstTransaction: new Date('2026-06-15'), },
          { address: '0x2222222222222222222222222222222222222222',
            dateOfFirstTransaction: new Date('2025-10-31'), },
          { address: '0x3333333333333333333333333333333333333333',
            dateOfFirstTransaction: new Date('2026-07-01'), },
        """)


def main():
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    cfg = yaml.safe_load(open(os.path.join(here, "config.yaml")))

    # --- sweep via Blockscout v2 (default path) ---
    ad = BaseChainAdapter(cfg, session=FakeBlockscout(), throttle=0, quiet=True)
    events = ad.fetch(limit=8)
    n_fac = len(cfg["facilitators"])
    assert len(events) == n_fac, f"expected {n_fac} (1 per facilitator), got {len(events)}"
    e = events[0]
    assert round(e.amount_usdc, 4) == 0.01
    assert e.payer_wallet == "0x" + PAYER and e.payee_wallet == "0x" + PAYEE
    assert e.ts.strftime("%Y-%m") == "2026-07"

    # --- wallet mode keeps outgoing only ---
    wev = ad.fetch_wallet("0x" + PAYER)
    assert len(wev) == 1 and round(wev[0].amount_usdc, 4) == 0.0125

    # --- refresh rewrites config with newest-first ---
    tmp = tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False)
    tmp.write(open(os.path.join(here, "config.yaml")).read()); tmp.close()
    refresh_facilitators(tmp.name, keep=2, session=FakeRegistry())
    new = yaml.safe_load(open(tmp.name))
    assert new["facilitators"] == [
        "0x3333333333333333333333333333333333333333",   # 2026-07-01
        "0x1111111111111111111111111111111111111111",   # 2026-06-15
    ], new["facilitators"]
    assert new.get("accounting"), "rest of config must survive the rewrite"
    os.unlink(tmp.name)

    print("ALL TESTS PASSED")
    print(f"  v2 sweep: {len(events)} events decoded, period {e.ts.strftime('%Y-%m')}")
    print("  wallet mode: outgoing-only filter ok")
    print("  refresh: newest-first rewrite ok, config intact")


if __name__ == "__main__":
    main()
