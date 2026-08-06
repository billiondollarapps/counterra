"""
Tests for crawl.py - the endpoint crawler.

Pins: URL extraction filters non-seller hosts and dedupes per host; the host
cooldown prevents re-hammering endpoints; resolved payTos become registry
entries carrying the exact provenance; already-known wallets are skipped.
Fake sessions throughout - no network, no payment ever.
"""
import datetime
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from counterralib import crawl as cw

ADDR = "0xfEf2e570b645EB720Ee6c589d27450810982f329"


class _Resp:
    def __init__(self, status=200, body="", jsonable=None, headers=None):
        self.status_code = status
        self.text = body
        self.headers = headers or {}
        self._json = jsonable

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError("HTTP %d" % self.status_code)

    def json(self):
        if self._json is None:
            raise ValueError("no json")
        return self._json


class _FakeSession:
    def __init__(self, routes):
        self.routes = routes

    def get(self, url, **kw):
        for key, resp in self.routes.items():
            if url.startswith(key):
                return resp
        return _Resp(404, "")

    def post(self, url, **kw):
        return _Resp(404, "")


def test_extract_urls_filters_and_dedupes():
    text = ("Try https://api.seller.ai/v1/search and also "
            "https://api.seller.ai/v1/other (same host, deduped). "
            "Repo: https://github.com/foo/bar badge https://img.shields.io/x "
            "and a real one http://tools.example.dev/run.")
    urls = cw.extract_urls(text)
    hosts = [cw._host(u) for u in urls]
    assert hosts == ["api.seller.ai", "tools.example.dev"], urls
    print("extract_urls filter + per-host dedup OK")


def test_cooldown_logic():
    today = datetime.date.today().isoformat()
    old = (datetime.date.today() - datetime.timedelta(days=30)).isoformat()
    state = {"fresh.example": today, "stale.example": old}
    assert not cw._fresh(state, "fresh.example", 7)
    assert cw._fresh(state, "stale.example", 7)
    assert cw._fresh(state, "never.example", 7)
    print("host cooldown OK")


def test_crawl_resolves_to_verified_entry():
    origin = "https://newseller.example"
    routes = {origin + "/.well-known/x402":
              _Resp(jsonable={"payTo": ADDR, "routes": []})}
    known = set()
    hits, probs = cw.crawl([origin + "/api/llm/generate"], known,
                           session=_FakeSession(routes), state={}, verbose=False)
    assert len(hits) == 1 and probs == [], (hits, probs)
    e = hits[0]
    assert e["wallet"] == ADDR.lower()
    assert e["label"] == "newseller.example"
    assert e["chain"] == "base"
    assert "payTo resolved from seller's own endpoint" in e["evidence"]
    assert "/.well-known/x402" in e["evidence"]          # provenance kept
    assert ADDR.lower() in known                         # dedup within run
    print("crawl -> verified entry with provenance OK")


def test_crawl_skips_known_wallets():
    origin = "https://oldseller.example"
    routes = {origin + "/.well-known/x402":
              _Resp(jsonable={"payTo": ADDR})}
    hits, probs = cw.crawl([origin], {ADDR.lower()},
                           session=_FakeSession(routes), state={}, verbose=False)
    assert hits == [] and probs == []
    print("crawl skips known wallets OK")


def test_crawl_respects_max_lookups_and_updates_state():
    routes = {}
    state = {}
    urls = ["https://a%d.example/x" % i for i in range(10)]
    cw.crawl(urls, set(), session=_FakeSession(routes), max_lookups=3,
             state=state, verbose=False)
    assert len(state) == 3, state
    print("max_lookups budget + state recording OK")


def test_harvest_github_shape():
    search = _Resp(jsonable={"items": [{"full_name": "foo/x402-thing"}]})
    readme = _Resp(body="endpoint: https://svc.example/api/v1 docs at "
                        "https://github.com/foo/x402-thing")
    sess = _FakeSession({cw.GITHUB_SEARCH: search,
                         "https://raw.githubusercontent.com/foo/x402-thing":
                         readme})
    urls = cw.harvest_github(session=sess, max_repos=5)
    assert urls == ["https://svc.example/api/v1"], urls
    print("github harvest -> README URL extraction OK")


def test_harvest_lists():
    sess = _FakeSession({"https://raw.list.example/awesome.md":
                         _Resp(body="* https://seller-one.example/api")})
    urls = cw.harvest_lists(["https://raw.list.example/awesome.md"],
                            session=sess)
    assert urls == ["https://seller-one.example/api"]
    print("list harvest OK")


def test_seeds_default_shape():
    seeds = cw.load_seeds(path="/nonexistent/seeds.json")
    assert seeds["github_search"] is True
    assert seeds["urls"] == [] and seeds["list_urls"] == []
    print("seeds default shape OK")



USDC_BASE = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"


def test_token_contract_rejected():
    # A page whose text mentions the USDC contract must NEVER become an entry.
    origin = "https://mentions-usdc.example"
    routes = {origin + "/llms.txt":
              _Resp(body="We settle in USDC: " + USDC_BASE)}
    hits, probs = cw.crawl([origin], set(),
                           session=_FakeSession(routes), state={},
                           verbose=False)
    assert hits == [] and probs == [], (hits, probs)
    print("token contract rejected OK")


def test_address_in_text_goes_to_probable():
    # A bare address in page text is circumstantial -> probable, not verified.
    origin = "https://textmention.example"
    routes = {origin + "/llms.txt":
              _Resp(body="our wallet happens to be " + ADDR + " thanks")}
    hits, probs = cw.crawl([origin], set(),
                           session=_FakeSession(routes), state={},
                           verbose=False)
    assert hits == [], hits
    assert len(probs) == 1 and probs[0]["wallet"] == ADDR.lower(), probs
    print("address-in-text -> probable queue OK")


def test_402_challenge_stays_verified():
    # A live 402 challenge is mechanical even when the body is plain text.
    url = "https://live402.example/api/run"
    routes = {("get", url): _Resp(status=402, body="pay " + ADDR)}
    hits, probs = cw.crawl([url], set(),
                           session=_FakeSession(routes), state={},
                           verbose=False)
    assert len(hits) == 1 and probs == [], (hits, probs)
    assert " 402 " in hits[0]["evidence"]
    print("live 402 challenge -> verified OK")


def test_private_hosts_filtered():
    urls = cw.extract_urls("see http://127.0.0.1:8080/x and "
                           "http://localhost.dev/x and http://192.168.1.5/x "
                           "and https://real.example/ok")
    assert urls == ["http://localhost.dev/x", "https://real.example/ok"] or \
           urls == ["https://real.example/ok"], urls
    print("private hosts filtered OK")


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
    print("\nALL CRAWL TESTS PASSED")
