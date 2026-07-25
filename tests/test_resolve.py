"""
Tests for resolve.py — URL -> payTo resolution.

Uses a fake session so tests never hit the network. Covers each resolution
path: well-known JSON with payTo, ownershipProofs list, CDP Bazaar merchant
link in text, and a live 402 challenge (header and body).
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from counterralib import resolve as rz

ADDR = "0xfEf2e570b645EB720Ee6c589d27450810982f329"


class _Resp:
    def __init__(self, status=200, body="", headers=None, jsonable=None):
        self.status_code = status
        self.text = body
        self.headers = headers or {}
        self._json = jsonable

    def json(self):
        if self._json is None:
            raise ValueError("no json")
        return self._json


class _FakeSession:
    """Maps URL (and method) -> _Resp. Missing URLs return 404."""
    def __init__(self, routes):
        self.routes = routes

    def get(self, url, **kw):
        return self.routes.get(("get", url)) or self.routes.get(url) or _Resp(404, "")

    def post(self, url, **kw):
        return self.routes.get(("post", url)) or _Resp(404, "")


def test_wellknown_json_payto():
    origin = "https://seller.example"
    routes = {origin + "/.well-known/x402":
              _Resp(jsonable={"payTo": ADDR, "routes": []})}
    out = rz.resolve_payto(origin, _FakeSession(routes))
    assert out and out["payto"] == ADDR, out
    assert out["source"].startswith("key:")
    print("well-known JSON payTo OK")


def test_ownership_proofs_list():
    origin = "https://seller2.example"
    routes = {origin + "/.well-known/x402":
              _Resp(jsonable={"ownershipProofs": [ADDR], "paid_routes": []})}
    out = rz.resolve_payto(origin, _FakeSession(routes))
    assert out["payto"] == ADDR and out["source"] == "ownershipProofs", out
    print("ownershipProofs list OK")


def test_bazaar_merchant_link_in_text():
    origin = "https://seller3.example"
    body = ("# Catalog\n- CDP Bazaar merchant: "
            "https://api.cdp.coinbase.com/platform/v2/x402/discovery/"
            f"merchant?payTo={ADDR}\n")
    # llms.txt is plain text, not JSON
    routes = {origin + "/llms.txt": _Resp(body=body)}
    out = rz.resolve_payto(origin, _FakeSession(routes))
    assert out["payto"] == ADDR, out
    assert out["source"] == "bazaar-merchant-link"
    print("bazaar merchant link in text OK")


def test_402_body():
    url = "https://seller4.example/x402/tool/run"
    origin = "https://seller4.example"
    # no well-known docs; the resource returns a 402 with payTo in the body
    routes = {("post", url): _Resp(402, jsonable={"accepts": [{"payTo": ADDR}]})}
    out = rz.resolve_payto(url, _FakeSession(routes))
    assert out and out["payto"] == ADDR, out
    assert "402 body" in out["detail"]
    print("live 402 body OK")


def test_402_header():
    url = "https://seller5.example/x402/tool/run"
    routes = {("get", url): _Resp(402, headers={"PAYMENT-REQUIRED": f"exact payTo={ADDR}"})}
    out = rz.resolve_payto(url, _FakeSession(routes))
    assert out and out["payto"] == ADDR, out
    assert "header" in out["detail"]
    print("live 402 header OK")


def test_priority_wellknown_beats_402():
    """A well-known doc should be used before hitting the live resource."""
    url = "https://seller6.example/x402/tool/run"
    origin = "https://seller6.example"
    routes = {
        origin + "/.well-known/x402": _Resp(jsonable={"payTo": ADDR}),
        ("post", url): _Resp(402, jsonable={"accepts": [{"payTo": "0x" + "9" * 40}]}),
    }
    out = rz.resolve_payto(url, _FakeSession(routes))
    assert out["payto"] == ADDR, "should prefer the well-known doc"
    print("resolution priority OK")


def test_nothing_found_returns_none():
    out = rz.resolve_payto("https://nowhere.example", _FakeSession({}))
    assert out is None
    print("no payTo -> None OK")


def test_origin_extraction():
    assert rz._origin("https://a.b/c/d") == "https://a.b"
    assert rz._origin("a.b") == "https://a.b"
    print("origin extraction OK")


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
    print("\nALL RESOLVE TESTS PASSED")
