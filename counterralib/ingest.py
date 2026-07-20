"""
Counterra ingestion layer.

Adapters produce canonical PaymentEvent records from a source.
- SampleDataAdapter: deterministic, realistic simulated x402 traffic (for the MVP demo).
- BaseChainAdapter: stub for live Base USDC data. Wire-up instructions in README.
"""
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta
import hashlib
import random


@dataclass
class PaymentEvent:
    tx_hash: str          # on-chain settlement reference
    ts: datetime          # timestamp
    chain: str            # e.g. "base"
    payer_wallet: str     # the agent's wallet / session key address
    payee_wallet: str     # the provider's receiving address
    amount_usdc: float    # settled amount in USDC
    protocol: str         # "x402"
    memo: str             # resource identifier if present (x402 v2 sessions)
    app_code: str = ""               # ERC-8021 'a': app that served the endpoint
    facilitator_code: str = ""       # ERC-8021 'w': facilitator that settled
    service_codes: str = ""          # ERC-8021 's': client-side code(s), comma-joined
    attribution_evidence: str = ""   # raw ERC-8021 suffix hex, for evidence trail

    def to_dict(self):
        d = asdict(self)
        d["ts"] = self.ts.isoformat()
        return d


class SampleDataAdapter:
    """
    Generates ~30 days of agent spending for one fictional company,
    shaped like observed x402 traffic:
      - heavy sub-cent API-call tail
      - mid-range data purchases ($0.25-$2)
      - occasional larger compute/report purchases ($5-$60)
      - facilitator batch settlement: many logical calls -> fewer on-chain txs
    Deterministic (seeded) so demo runs are reproducible.
    """

    PROVIDERS = [
        # (wallet, label, category, amount profile)
        ("0xA11ce00000000000000000000000000000000001", "MarketFeed API",   "Market data",      "micro"),
        ("0xB0b0000000000000000000000000000000000002", "GeoTiles API",     "Mapping data",     "micro"),
        ("0xC0ffee0000000000000000000000000000000003", "LLM Inference Co", "AI inference",     "mid"),
        ("0xD00d000000000000000000000000000000000004", "DocParse API",     "Document parsing", "micro"),
        ("0xE55e000000000000000000000000000000000005", "CreditSignals",    "Risk data",        "mid"),
        ("0xF00d000000000000000000000000000000000006", "RenderFarm GPU",   "Compute",          "large"),
        ("0xAB1e000000000000000000000000000000000007", "NewsWire Pro",     "Market data",      "mid"),
        ("0xBEEF000000000000000000000000000000000008", "0xf7A9e...unknown", None,              "mid"),  # unmapped: exception path
    ]

    AGENTS = [
        ("0x1111000000000000000000000000000000000aa1", "research-agent"),
        ("0x2222000000000000000000000000000000000aa2", "procurement-agent"),
        ("0x3333000000000000000000000000000000000aa3", "reporting-agent"),
    ]

    def __init__(self, days: int = 30, seed: int = 42):
        self.days = days
        self.rng = random.Random(seed)

    def _amount(self, profile: str) -> float:
        r = self.rng
        if profile == "micro":
            return round(r.choice([0.001, 0.002, 0.005, 0.01]) * r.randint(1, 3), 6)
        if profile == "mid":
            return round(r.uniform(0.25, 2.0), 4)
        return round(r.uniform(5, 60), 2)

    def fetch(self):
        events = []
        start = datetime(2026, 6, 8)
        for day in range(self.days):
            date = start + timedelta(days=day)
            for payer, agent_name in self.AGENTS:
                # each agent's daily activity varies
                n_calls = self.rng.randint(120, 900) if agent_name == "research-agent" else self.rng.randint(30, 300)
                # facilitator batches: ~40 logical calls settle as one on-chain tx
                batches = max(1, n_calls // 40)
                for b in range(batches):
                    prov = self.rng.choices(
                        self.PROVIDERS,
                        weights=[30, 18, 14, 16, 6, 2, 8, 6],
                    )[0]
                    wallet, label, cat, profile = prov
                    calls_in_batch = self.rng.randint(20, 60) if profile == "micro" else self.rng.randint(1, 4)
                    amt = round(sum(self._amount(profile) for _ in range(calls_in_batch)), 6)
                    ts = date + timedelta(minutes=self.rng.randint(0, 1439))
                    h = hashlib.sha256(f"{ts}{payer}{wallet}{b}{amt}".encode()).hexdigest()
                    events.append(PaymentEvent(
                        tx_hash="0x" + h[:40],
                        ts=ts,
                        chain="base",
                        payer_wallet=payer,
                        payee_wallet=wallet,
                        amount_usdc=amt,
                        protocol="x402",
                        memo=f"{calls_in_batch} calls",
                    ))
        events.sort(key=lambda e: e.ts)
        return events


class BaseChainAdapter:
    """
    LIVE-DATA STUB.
    To wire real Base data (next session, on your machine):
      1. Get a free API key from Alchemy or Basescan.
      2. Query USDC Transfer events where `to` or `from` is your tracked wallet
         (USDC on Base: 0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913).
      3. Map each transfer into PaymentEvent and return the list.
    The rest of the pipeline needs no changes.
    """
    def fetch(self):
        raise NotImplementedError("Wire an RPC/indexer API key - see README step 2.")