"""
Tests for enrich.py - on-chain name enrichment and evidence-tier assignment.

The tier rules are the trust boundary of the whole autogrow system:
owner-declared signals (Basename, verified contract name) may auto-publish;
community tags must queue. These tests pin that behaviour. Fake session,
no network.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from counterralib import enrich as en

ADDR = "0x" + "ab" * 20


class _Resp:
    def __init__(self, status=200, jsonable=None):
        self.status_code = status
        self._json = jsonable

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError("HTTP %d" % self.status_code)

    def json(self):
        return self._json


class _FakeSession:
    def __init__(self, payload, status=200):
        self.payload = payload
        self.status = status

    def get(self, url, **kw):
        return _Resp(self.status, self.payload)


def test_basename_is_verified_tier():
    s = _FakeSession({"ens_domain_name": "payments.someservice.base.eth",
                      "is_contract": False, "public_tags": []})
    info = en.enrich_wallet(ADDR, session=s)
    cand = en.enrichment_candidate(ADDR, info)
    assert cand["tier"] == "verified", cand
    assert cand["label"] == "payments.someservice.base.eth"
    assert "Basename" in cand["evidence"]
    print("basename -> verified OK")


def test_contract_name_is_verified_tier():
    s = _FakeSession({"ens_domain_name": None, "is_contract": True,
                      "name": "InferencePaymaster", "public_tags": []})
    info = en.enrich_wallet(ADDR, session=s)
    cand = en.enrichment_candidate(ADDR, info)
    assert cand["tier"] == "verified"
    assert cand["label"] == "InferencePaymaster"
    print("verified contract name -> verified OK")


def test_public_tag_is_probable_tier():
    s = _FakeSession({"ens_domain_name": None, "is_contract": False,
                      "public_tags": [{"display_name": "Some LLM Gateway"}]})
    info = en.enrich_wallet(ADDR, session=s)
    cand = en.enrichment_candidate(ADDR, info)
    assert cand["tier"] == "probable", cand
    assert "review" in cand["evidence"]
    print("public tag -> probable (queued, never auto-published) OK")


def test_basename_outranks_tags():
    s = _FakeSession({"ens_domain_name": "svc.base.eth", "is_contract": True,
                      "name": "Foo", "public_tags": [{"name": "bar"}]})
    cand = en.enrichment_candidate(ADDR, en.enrich_wallet(ADDR, session=s))
    assert cand["tier"] == "verified" and cand["label"] == "svc.base.eth"
    print("signal priority (basename first) OK")


def test_nothing_nameable_returns_none():
    s = _FakeSession({"ens_domain_name": None, "is_contract": False,
                      "public_tags": []})
    info = en.enrich_wallet(ADDR, session=s)
    assert en.enrichment_candidate(ADDR, info) is None
    print("nothing nameable -> None OK")


def test_eoa_name_field_ignored():
    # Blockscout sometimes puts junk in `name` for EOAs; only contracts count.
    s = _FakeSession({"ens_domain_name": None, "is_contract": False,
                      "name": "NotAContract", "public_tags": []})
    info = en.enrich_wallet(ADDR, session=s)
    assert info["contract_name"] is None
    print("EOA name field ignored OK")


def test_lookup_failure_returns_none():
    assert en.enrich_wallet(ADDR, session=_FakeSession(None, status=500)) is None
    assert en.enrich_wallet("SolanaAddrNotEvm", session=None) is None
    print("failed/non-EVM lookup -> None OK")


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
    print("\nALL ENRICH TESTS PASSED")
