"""Tests for erc8021.py - vectors constructed per the published spec."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import cbor2
import counterralib.erc8021 as erc8021
from counterralib.erc8021 import parse_attribution, ERC8021_MARKER

MARKER = ERC8021_MARKER.hex()


def make_schema2(a=None, w=None, s=None, prefix="a9059cbb" + "00" * 64):
    m = {}
    if a: m["a"] = a
    if w: m["w"] = w
    if s is not None: m["s"] = s
    cbor = cbor2.dumps(m)
    suffix = cbor + len(cbor).to_bytes(2, "big") + b"\x02" + ERC8021_MARKER
    return prefix + suffix.hex()


def test_official_schema0_vector():
    # From blog.base.dev: schemaData "baseapp"(7 ascii bytes) + 07, schemaId 00, marker
    calldata = "0xabcd1234" + "62617365617070" + "07" + "00" + MARKER
    a = parse_attribution(calldata)
    assert a is not None and a.schema_id == 0
    assert a.codes == ["baseapp"], a.codes
    print("schema0 official vector OK:", a.codes)


def test_schema0_multi_codes():
    codes = b"myapp,otherapp"
    calldata = "0x" + "deadbeef" + codes.hex() + f"{len(codes):02x}" + "00" + MARKER
    a = parse_attribution(calldata)
    assert a.codes == ["myapp", "otherapp"], a.codes
    print("schema0 multi-code OK:", a.codes)


def test_schema2_all_fields():
    calldata = "0x" + make_schema2(a="counterra", w="cdp_facilitator", s=["agent_client"])
    a = parse_attribution(calldata)
    assert a is not None and a.schema_id == 2
    assert a.app_code == "counterra"
    assert a.facilitator_code == "cdp_facilitator"
    assert a.service_codes == ["agent_client"]
    assert sorted(a.all_codes()) == ["agent_client", "cdp_facilitator", "counterra"]
    print("schema2 full OK:", a.app_code, a.facilitator_code, a.service_codes)


def test_schema2_s_as_string():
    calldata = "0x" + make_schema2(a="someapp", s="solo_client")
    a = parse_attribution(calldata)
    assert a.service_codes == ["solo_client"]
    assert a.facilitator_code is None
    print("schema2 string-s OK")


def test_schema2_minimal_decoder_matches_cbor2():
    # Force the built-in decoder path and compare against cbor2 result
    m = {"a": "bc_7drupjtb", "w": "base_fac", "s": ["x", "long_client_name_here"]}
    data = cbor2.dumps(m)
    decoded, end = erc8021._cbor_decode_item(data, 0)
    assert end == len(data) and decoded == m, decoded
    print("minimal CBOR decoder parity OK")


def test_no_suffix():
    assert parse_attribution("0xa9059cbb" + "00" * 64) is None
    assert parse_attribution("0x") is None
    assert parse_attribution("") is None
    print("no-suffix cases OK")


def test_garbage_resilience():
    # Marker present but corrupt CBOR must not raise
    bad = "0x" + "ff" * 10 + "0005" + "02" + MARKER  # claims 5 CBOR bytes of 0xff
    assert parse_attribution(bad) is None
    # Truncated calldata shorter than marker
    assert parse_attribution("0x8021") is None
    # Odd-length hex
    assert parse_attribution("0xabc") is None
    print("garbage resilience OK")


def test_unknown_schema_reported():
    calldata = "0x" + "aa" * 8 + "07" + MARKER  # schemaId 0x07, unknown
    a = parse_attribution(calldata)
    assert a is not None and a.schema_id == 7 and a.all_codes() == []
    print("unknown schema reported OK")


def test_invalid_code_chars_filtered():
    calldata = "0x" + make_schema2(a="Bad-Code!", s=["good_code", "ALSO BAD"])
    a = parse_attribution(calldata)
    assert a is not None
    assert a.app_code is None
    assert a.service_codes == ["good_code"]
    print("invalid-code filtering OK")


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
    print("\nALL TESTS PASSED")
