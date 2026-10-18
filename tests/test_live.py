"""
Offline tests for the live adapter + facilitator refresh.
No network, no API key: canned responses shaped like the real APIs.
Run:  python3 tests/test_live.py
"""
import sys, os, json, tempfile
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import yaml
from counterralib.live import BaseChainAdapter, refresh_facilitators, TRANSFER_TOPIC

USDC = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
PAYER = "1111000000000000000000000000000000000aa1"
PAYEE = "a11ce00000000000000000000000000000000001"


class FakeResp:
    status_code = 200

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

    # --- Permit2 / batch-settlement paths (the October 2026 Base stall) ---
    permit2_and_batch(cfg)

    print("ALL TESTS PASSED")
    print(f"  v2 sweep: {len(events)} events decoded, period {e.ts.strftime('%Y-%m')}")
    print("  wallet mode: outgoing-only filter ok")
    print("  refresh: newest-first rewrite ok, config intact")


PROXY = "0x402085c248eea27d92e8b30b2c58ed07f9e20001"     # x402ExactPermit2Proxy
ESCROW = "0x4020074e9df2ce1dee5a9c1b5c3f541d02a10003"    # x402BatchSettlement
COLLECTOR = "0x4020806089470a89826cb9fb1f4059150b550004"
SELLER2 = "b0b0000000000000000000000000000000000002"
FAC = "0x2a89407a98a0732b7fd578c4e156b7166540eb5a"


def _t(a):
    return "0x" + a.replace("0x", "").rjust(64, "0")


def _log(frm, to, units):
    return {"address": {"hash": USDC}, "topics": [TRANSFER_TOPIC, _t(frm), _t(to)],
            "data": hex(units)}


class FakeMixedBlockscout:
    """One facilitator whose txs settle four different ways."""
    LOGS = {
        "0xp1": [_log(PAYER, PAYEE, 20000)],                          # via Permit2 proxy
        "0xd1": [_log(PAYER, COLLECTOR, 5000000),                     # deposit -> collector
                 _log(COLLECTOR, ESCROW, 5000000)],                   # collector -> escrow hop
        "0xs1": [_log(ESCROW, SELLER2, 1250000)],                     # escrow settles to seller
        "0xf1": [_log(FAC, PAYEE, 1)],                                # facilitator's own money
    }

    def get(self, url, params=None, timeout=None):
        if "/addresses/" in url and "/transactions" in url:
            return FakeResp({"items": [
                {"hash": "0xp1", "timestamp": "2026-10-10T10:00:00Z", "to": {"hash": PROXY}, "status": "ok"},
                {"hash": "0xd1", "timestamp": "2026-10-10T10:01:00Z", "to": {"hash": ESCROW}, "status": "ok"},
                {"hash": "0xs1", "timestamp": "2026-10-10T10:02:00Z", "to": {"hash": ESCROW}, "status": "ok"},
                {"hash": "0xf1", "timestamp": "2026-10-10T10:03:00Z", "to": {"hash": USDC}, "status": "ok"},
                {"hash": "0xx1", "timestamp": "2026-10-10T10:04:00Z",
                 "to": {"hash": "0x00000000000000000000000000000000000000ff"}, "status": "ok"},
            ], "next_page_params": None})
        if "/logs" in url:
            h = url.split("/transactions/")[1].split("/")[0]
            return FakeResp({"items": self.LOGS[h]})
        raise AssertionError(f"unexpected url {url}")


def permit2_and_batch(cfg):
    ad = BaseChainAdapter(cfg, session=FakeMixedBlockscout(), throttle=0, quiet=True)
    census = {"wallets": [{"chain": "base", "address": FAC, "name": "Coinbase",
                           "facilitator": "coinbase", "deprecated": False,
                           "first_tx": "2026-06-15"}]}
    sched = {}
    assert ad.use_census(census, schedule=sched, per_run=5)
    evs = ad.fetch(limit=10)
    legs = sorted((e.memo, e.payee_wallet[-4:], round(e.amount_usdc, 4)) for e in evs)
    assert legs == [("x402:batch-deposit", COLLECTOR[-4:], 5.0),
                    ("x402:batch-payout", SELLER2[-4:], 1.25),
                    ("x402:permit2", PAYEE[-4:], 0.02)], legs
    assert all(e.facilitator == "Coinbase" for e in evs)
    assert ad.diag["listed"] == 5 and ad.diag["other_targets"], ad.diag
    assert sched[FAC]["last_listed"] == 4 and sched[FAC]["rate_per_hour"] > 0, sched
    print("  permit2 + batch-settlement legs typed; facilitator self-transfer skipped; "
          "census schedule recorded")


if __name__ == "__main__":
    main()
