"""
Tests for whois.catalog_index / identify_from_index - the fetch-once batch
path autogrow uses. Pins: the index maps payTo -> resources, matches are
case-insensitive for EVM, and identify_from_index agrees with identify()'s
output shape. Fake session, no network.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from counterralib import whois as wh

ADDR = "0xAbCd000000000000000000000000000000000001"


class _Resp:
    def __init__(self, jsonable):
        self._json = jsonable
        self.status_code = 200

    def raise_for_status(self):
        pass

    def json(self):
        return self._json


class _FakeSession:
    """First discovery endpoint returns one page with one item."""
    def __init__(self):
        self.calls = 0

    def get(self, url, params=None, **kw):
        self.calls += 1
        if (params or {}).get("offset", 0) == 0 and self.calls <= 1:
            return _Resp({"items": [
                {"resource": {"url": "https://svc.example/api/llm",
                              "description": "inference"},
                 "accepts": [{"payTo": ADDR}]}],
                "pagination": {"total": 1, "limit": 100}})
        return _Resp({"items": [], "pagination": {"total": 1, "limit": 100}})


def test_index_and_match():
    idx = wh.catalog_index(session=_FakeSession())
    assert ADDR.lower() in idx, idx
    out = wh.identify_from_index(ADDR.lower(), idx)
    assert out["label"] == "svc.example"
    assert out["category_suggestion"] == "AI inference"
    assert "Discovery catalog payTo match: 1 resources" in out["evidence"]
    print("catalog index + match OK")


def test_miss_returns_empty():
    out = wh.identify_from_index("0x" + "9" * 40, {})
    assert out["label"] is None and out["matches"] == []
    print("index miss -> empty ident OK")


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
    print("\nALL CATALOG-INDEX TESTS PASSED")
