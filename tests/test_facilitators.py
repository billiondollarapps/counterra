"""
Offline tests for the facilitator census + sweep scheduler.
Run:  python3 tests/test_facilitators.py
"""
import datetime
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from counterralib import facilitators as F

INDEX = """
export { auto, autoFacilitator } from './auto';
export { coinbase, coinbaseFacilitator } from './coinbase';
export { payai, payaiFacilitator } from './payai';
export { coinbase as again } from './coinbase';
"""

COINBASE = """
export const coinbaseFacilitator = {
  id: 'coinbase',
  metadata: {
    name: 'Coinbase',
    image: 'https://x402scan.com/coinbase.png',
  },
  addresses: {
    [Network.BASE]: [
      {
        address: '0xDBDF3D8ED80F84C35D01C6C9F9271761BAD90BA6',
        deprecated: true,
        tokens: [USDC_BASE_TOKEN],
        dateOfFirstTransaction: new Date('2025-05-05'),
      },
      {
        address: '0x2a89407a98a0732b7fd578c4e156b7166540eb5a',
        tokens: [USDC_BASE_TOKEN],
        dateOfFirstTransaction: new Date('2026-06-15'),
      },
    ],
    [Network.SOLANA]: [
      {
        address: 'BENrLoUbndxoNMUS5JXApGMtNykLjFXXixMtpDwDR9SP',
        tokens: [USDC_SOLANA_TOKEN],
        dateOfFirstTransaction: new Date('2025-12-16'),
      },
    ],
  },
} as const satisfies Facilitator;
"""

PAYAI = """
export const payaiFacilitator = {
  id: 'payAI',
  metadata: { name: 'PayAI', image: 'x' },
  addresses: {
    [Network.SOLANA]: [
      {
        address: '2wKupLR9q6wXYppw8Gr2NvWxKBUqm4PPJKkQfoxHDBg4',
        tokens: [USDC_SOLANA_TOKEN],
        dateOfFirstTransaction: new Date('2025-07-01'),
      },
    ],
  },
};
"""

OLD = """
export const oldFacilitator = {
  id: 'old',
  metadata: { name: 'Old', image: 'x' },
  deprecated: true,
  addresses: {
    [Network.BASE]: [
      {
        address: '0x1111111111111111111111111111111111111111',
        tokens: [USDC_BASE_TOKEN],
        dateOfFirstTransaction: new Date('2025-01-01'),
      },
    ],
  },
};
"""


class Resp:
    def __init__(self, text, code=200):
        self.text, self.status_code = text, code


class FakeGH:
    FILES = {"index.ts": INDEX, "coinbase.ts": COINBASE, "payai.ts": PAYAI}

    def get(self, url, timeout=None):
        name = url.rsplit("/", 1)[1]
        if name in self.FILES:
            return Resp(self.FILES[name])
        return Resp("", 404)


def main():
    assert F.parse_index(INDEX) == ["auto", "coinbase", "payai"]

    rows = F.parse_facilitator_ts(COINBASE, "coinbase")
    assert [(r["chain"], r["deprecated"]) for r in rows] == [
        ("base", True), ("base", False), ("solana", False)], rows
    assert rows[0]["address"] == "0xdbdf3d8ed80f84c35d01c6c9f9271761bad90ba6", "EVM lowercased"
    assert rows[2]["address"] == "BENrLoUbndxoNMUS5JXApGMtNykLjFXXixMtpDwDR9SP", "base58 kept"
    assert rows[1]["name"] == "Coinbase" and rows[1]["first_tx"] == "2026-06-15"
    assert all(r["deprecated"] for r in F.parse_facilitator_ts(OLD, "old")), \
        "top-level deprecated marks every wallet"

    census = F.fetch_census(session=FakeGH())
    assert census["failed_modules"] == ["auto"], census["failed_modules"]
    assert census["summary"]["base"]["live"] == 1
    assert census["summary"]["solana"]["live"] == 2
    assert sorted(census["summary"]["solana"]["facilitators"]) == ["Coinbase", "PayAI"]
    live_sol = [w["address"] for w in F.live_wallets(census, "solana")]
    assert len(live_sol) == 2
    assert "0xdbdf3d8ed80f84c35d01c6c9f9271761bad90ba6" in F.facilitator_index(census), \
        "deprecated wallets stay labelled as facilitators"

    # --- scheduler: never-visited first, then busiest x staleness ---
    now = datetime.datetime(2026, 10, 18, 12, tzinfo=datetime.timezone.utc)
    ago = lambda h: (now - datetime.timedelta(hours=h)).isoformat()
    state = {
        "busy": {"last_visit": ago(2), "rate_per_hour": 30.0},    # score 60
        "quiet": {"last_visit": ago(40), "rate_per_hour": 0.0},   # floor 0.05*40 = 2
        "medium": {"last_visit": ago(10), "rate_per_hour": 3.0},  # score 30
    }
    picked = F.pick_wallets(["busy", "quiet", "medium", "new"], state, 3, now=now)
    assert picked == ["new", "busy", "medium"], picked

    F.record_visit(state, "new", 50, ago(0), ago(5), now=now)
    assert abs(state["new"]["rate_per_hour"] - 10.0) < 1e-6, state["new"]
    F.record_visit(state, "busy", 0, None, None, now=now)
    assert state["busy"]["rate_per_hour"] == 15.0, "empty visit decays the rate by half"

    assert F.contract_info("0x4020074E9DF2CE1DEE5A9C1B5C3F541D02A10003")[1] == "batch-escrow"

    print("ALL FACILITATOR TESTS PASSED")
    print("  registry parse: live/deprecated/top-level-deprecated, EVM vs base58 casing")
    print("  census: missing module reported, deprecated wallets still indexed")
    print("  scheduler: unvisited first, then rate x staleness; empty visit decays")


if __name__ == "__main__":
    main()
