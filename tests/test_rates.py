"""
Offline tests for daily exchange rates (counterralib/rates.py).
Run:  python3 tests/test_rates.py
"""
import datetime
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from counterralib import rates as R


def ms(day, hour):
    d = datetime.datetime.fromisoformat(day).replace(hour=hour, tzinfo=datetime.timezone.utc)
    return int(d.timestamp() * 1000)


class Resp:
    def __init__(self, payload, code=200):
        self.payload, self.status_code, self.text = payload, code, ""

    def json(self):
        return self.payload


class FakeGecko:
    def __init__(self, fail_first=0):
        self.calls, self.fail_first = [], fail_first

    def get(self, url, params=None, headers=None, timeout=None):
        self.calls.append((url, params, headers))
        if self.fail_first:
            self.fail_first -= 1
            return Resp({}, 429)
        a = datetime.datetime.fromtimestamp(params["from"], tz=datetime.timezone.utc).date()
        b = datetime.datetime.fromtimestamp(params["to"], tz=datetime.timezone.utc).date()
        pts, d = [], a
        while d <= b:
            if d.isoformat() == "2026-09-08":          # a depeg-ish day
                pts += [[ms("2026-09-08", h), 0.996 if h < 12 else 1.0] for h in range(24)]
            else:
                pts += [[ms(d.isoformat(), h), 1.0001] for h in range(24)]
            d += datetime.timedelta(days=1)
        return Resp({"prices": pts, "market_caps": [], "total_volumes": []})


def main():
    m = R.daily_means([[ms("2026-09-08", 0), 0.99], [ms("2026-09-08", 23), 1.01], [ms("2026-09-09", 1), 1.0]])
    assert m["2026-09-08"] == {"rate": 1.0, "points": 2} and m["2026-09-09"]["points"] == 1

    s = FakeGecko()
    got = R.fetch("USDC", datetime.date(2026, 6, 1), datetime.date(2026, 9, 30), session=s,
                  api_key="demo-key", sleep=0)
    assert len(s.calls) == 2, "122 days -> two <=89-day windows, keeping hourly granularity"
    assert s.calls[0][2]["x-cg-demo-api-key"] == "demo-key"
    assert "usd-coin" in s.calls[0][0]
    assert got["2026-09-08"] == {"rate": 0.998, "points": 24}
    assert len(got) == 122

    # update(): only completed days inside the 365-day window; cached thereafter
    d = tempfile.mkdtemp()
    p = os.path.join(d, "rates.json")
    today = datetime.date(2026, 10, 1)
    s2 = FakeGecko(fail_first=1)
    import counterralib.rates as mod
    real = mod.time.sleep
    mod.time.sleep = lambda x: None
    try:
        r = R.update(["2026-09-08", "2026-09-09", "2026-10-01", "2025-01-01"], path=p,
                     session=s2, today=today)
        assert r == {"needed": 2, "fetched": 2, "missing": []}, r
        assert s2.calls and len(s2.calls) == 2, "one 429 retried, then success"
        r2 = R.update(["2026-09-08"], path=p, session=FakeGecko(), today=today)
        assert r2["needed"] == 0, "cached days are never fetched twice"
    finally:
        mod.time.sleep = real
    t = json.load(open(p))
    assert t["tokens"]["USDC"]["2026-09-08"]["rate"] == 0.998

    rb = R.RateBook(t)
    assert rb.rate("USDC", "2026-09-08") == 0.998
    assert rb.rate("USDC", "2026-09-20") == 1.0 and ("USDC", "2026-09-20") in rb.fallbacks
    assert rb.info("USDC", "2026-09-20")["source"].startswith("PAR FALLBACK")

    print("ALL RATES TESTS PASSED")
    print("  hourly windows <90 days averaged per UTC day; demo key header; 429 retried")
    print("  cache: completed days only, 365-day limit, never refetched; par fallback recorded")


if __name__ == "__main__":
    main()
