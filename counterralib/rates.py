"""
Daily exchange rates - token amounts translated to USD the way forex is.

Every x402 payment is recorded in the token that actually moved (USDC today)
and translated to the reporting currency (USD) at that UTC day's AVERAGE
rate - the same practice used for rupee, euro or any foreign-currency
transaction. Stablecoins sit near 1.00, but not exactly (USDC fell to ~0.88
in March 2023), so par is never silently assumed.

Source: CoinGecko /coins/{id}/market_chart/range, fetched in <= 90-day
windows so the API returns HOURLY prices; the daily rate is the mean of that
UTC day's hourly points. Cached in out/rates.json so each day is fetched once.
Optional free key: COINGECKO_API_KEY (demo plan; history limited to 365 days).

When a day has no rate, callers fall back to 1.0 and MUST report it as an
exception - the pack says so; nothing is hidden.

Python 3.8-safe.
"""

from __future__ import annotations

import datetime
import json
import os
import time
from collections import defaultdict
from typing import Dict, Iterable, Optional, Tuple

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RATES_PATH = os.path.join(HERE, "out", "rates.json")
API = "https://api.coingecko.com/api/v3/coins/{id}/market_chart/range"
WINDOW_DAYS = 89                  # < 90 keeps hourly granularity

# token symbol -> CoinGecko id. USDC is the same asset on Base and Solana.
TOKENS = {"USDC": "usd-coin"}
REPORTING = "USD"


def token_of(chain: str) -> str:
    """Every settlement Counterra decodes today is USDC (Base and Solana)."""
    return "USDC"


def load(path: Optional[str] = None) -> dict:
    try:
        with open(path or RATES_PATH) as f:
            return json.load(f)
    except Exception:
        return {"reporting": REPORTING, "source": "coingecko", "tokens": {}}


def save(table: dict, path: Optional[str] = None):
    p = path or RATES_PATH
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w") as f:
        json.dump(table, f, indent=1, sort_keys=True)
        f.write("\n")


def daily_means(points: Iterable[Tuple[float, float]]) -> Dict[str, dict]:
    """[[ms, price], ...] -> {YYYY-MM-DD: {rate, points}} (UTC days)."""
    by = defaultdict(list)
    for ms, px in points:
        d = datetime.datetime.fromtimestamp(ms / 1000.0, tz=datetime.timezone.utc).date().isoformat()
        by[d].append(float(px))
    return {d: {"rate": round(sum(v) / len(v), 8), "points": len(v)} for d, v in by.items()}


def fetch(token: str, start: datetime.date, end: datetime.date, session=None,
          api_key: Optional[str] = None, sleep: float = 2.5) -> Dict[str, dict]:
    """Daily mean rates for [start, end] from CoinGecko, in hourly windows."""
    if session is None:
        import requests
        session = requests.Session()
    cid = TOKENS[token]
    key = api_key if api_key is not None else os.environ.get("COINGECKO_API_KEY", "")
    headers = {"accept": "application/json", "User-Agent": "counterra-rates/1.0"}
    if key:
        headers["x-cg-demo-api-key"] = key
    out: Dict[str, dict] = {}
    a = start
    while a <= end:
        b = min(end, a + datetime.timedelta(days=WINDOW_DAYS - 1))
        frm = int(datetime.datetime(a.year, a.month, a.day, tzinfo=datetime.timezone.utc).timestamp())
        to = int(datetime.datetime(b.year, b.month, b.day, 23, 59, 59, tzinfo=datetime.timezone.utc).timestamp())
        for attempt in range(4):
            r = session.get(API.format(id=cid), params={"vs_currency": "usd", "from": frm, "to": to},
                            headers=headers, timeout=30)
            code = getattr(r, "status_code", 200)
            if code == 429:
                time.sleep(15 * (attempt + 1))
                continue
            if code >= 400:
                raise RuntimeError("CoinGecko HTTP {}: {}".format(code, (getattr(r, "text", "") or "")[:160]))
            for d, v in daily_means(r.json().get("prices") or []).items():
                if a.isoformat() <= d <= b.isoformat():
                    out[d] = v
            break
        else:
            raise RuntimeError("CoinGecko rate limited - try again later or set COINGECKO_API_KEY")
        a = b + datetime.timedelta(days=1)
        time.sleep(sleep)
    return out


def update(days_needed: Iterable[str], token: str = "USDC", path: Optional[str] = None,
           session=None, today: Optional[datetime.date] = None) -> dict:
    """Fetch any missing COMPLETED days (today is never final) into the cache."""
    table = load(path)
    have = table.setdefault("tokens", {}).setdefault(token, {})
    today = today or datetime.datetime.now(datetime.timezone.utc).date()
    floor = today - datetime.timedelta(days=364)       # demo-plan history limit
    need = sorted({d for d in days_needed
                   if d not in have and floor.isoformat() <= d < today.isoformat()})
    fetched = {}
    if need:
        fetched = fetch(token, datetime.date.fromisoformat(need[0]),
                        datetime.date.fromisoformat(need[-1]), session=session)
        for d, v in fetched.items():
            if d in need or d not in have:
                have[d] = dict(v, source="coingecko hourly mean")
        table["updated_at"] = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
        save(table, path)
    return {"needed": len(need), "fetched": len([d for d in need if d in fetched]),
            "missing": [d for d in need if d not in fetched]}


class RateBook:
    """Lookup with explicit fallback accounting."""

    def __init__(self, table: Optional[dict] = None):
        self.table = table if table is not None else load()
        self.fallbacks = set()

    def rate(self, token: str, day: str) -> float:
        v = ((self.table.get("tokens") or {}).get(token) or {}).get(day)
        if v and v.get("rate"):
            return float(v["rate"])
        self.fallbacks.add((token, day))
        return 1.0

    def info(self, token: str, day: str) -> dict:
        v = ((self.table.get("tokens") or {}).get(token) or {}).get(day)
        if v and v.get("rate"):
            return {"rate": float(v["rate"]), "points": v.get("points"),
                    "source": v.get("source", "coingecko")}
        return {"rate": 1.0, "points": 0, "source": "PAR FALLBACK - rate unavailable"}
