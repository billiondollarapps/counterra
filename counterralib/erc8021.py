"""
erc8021.py — ERC-8021 attribution-suffix parser for Counterra.

Parses builder-code attribution data appended to transaction calldata,
per the ERC-8021 standard (draft, Conner Swenberg) as deployed on Base.

Supported schemas:
  Schema 0 — generic builder codes:
      wire order (start -> end): [codes ascii, comma-separated][codesLength 1B][0x00][marker 16B]
  Schema 2 — x402 builder-code extension (CBOR):
      wire order (start -> end): [cborData][cborLength 2B big-endian][0x02][marker 16B]
      cborData is a CBOR map with optional fields:
        a: app code (resource server / seller-side app)   -> str
        w: wallet code (facilitator that settled)          -> str
        s: service code(s) (client / agent-side)           -> str | [str]

The suffix is parsed BACKWARDS from the end of calldata:
  last 16 bytes must equal the marker 0x80218021802180218021802180218021,
  the byte before is the schemaId, preceding bytes are schema data.

Dependency-free: includes a minimal CBOR decoder sufficient for the
schema-2 payload (text strings, arrays, maps, small ints). Falls back to
the cbor2 package if installed, for robustness on exotic encodings.

Reference: docs.x402.org/extensions/builder-code, blog.base.dev ERC-8021 post.
Builder codes match ^[a-z0-9_]{1,32}$.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Optional, Union

ERC8021_MARKER = bytes.fromhex("80218021802180218021802180218021")
SCHEMA_GENERIC = 0x00
SCHEMA_X402_BUILDER_CODE = 0x02
CODE_PATTERN = re.compile(r"^[a-z0-9_]{1,32}$")


@dataclass
class Attribution:
    """Parsed ERC-8021 attribution from a settlement's calldata."""

    schema_id: int
    # Schema 2 (x402) fields
    app_code: Optional[str] = None          # 'a' — seller-side app that exposed the endpoint
    facilitator_code: Optional[str] = None  # 'w' — facilitator that settled on-chain
    service_codes: list[str] = field(default_factory=list)  # 's' — client/agent-side code(s)
    # Schema 0 fields
    codes: list[str] = field(default_factory=list)
    # Raw suffix bytes (marker + schema id + data), for evidence storage
    raw_suffix_hex: str = ""

    def all_codes(self) -> list[str]:
        """Every attribution code present, regardless of schema/role."""
        out = list(self.codes)
        if self.app_code:
            out.append(self.app_code)
        if self.facilitator_code:
            out.append(self.facilitator_code)
        out.extend(self.service_codes)
        return out


# ---------------------------------------------------------------------------
# Minimal CBOR decoder (major types 0-5, definite lengths) — enough for the
# {a, w, s} map produced by the x402 builder-code extension.
# ---------------------------------------------------------------------------

class _CborError(ValueError):
    pass


def _cbor_read_length(data: bytes, pos: int, info: int) -> tuple[int, int]:
    if info < 24:
        return info, pos
    if info == 24:
        if pos >= len(data):
            raise _CborError("truncated uint8 length")
        return data[pos], pos + 1
    if info == 25:
        if pos + 2 > len(data):
            raise _CborError("truncated uint16 length")
        return int.from_bytes(data[pos:pos + 2], "big"), pos + 2
    if info == 26:
        if pos + 4 > len(data):
            raise _CborError("truncated uint32 length")
        return int.from_bytes(data[pos:pos + 4], "big"), pos + 4
    raise _CborError(f"unsupported CBOR additional info {info}")


def _cbor_decode_item(data: bytes, pos: int) -> tuple[Any, int]:
    if pos >= len(data):
        raise _CborError("truncated CBOR item")
    initial = data[pos]
    pos += 1
    major, info = initial >> 5, initial & 0x1F

    if major == 0:  # unsigned int
        return _cbor_read_length(data, pos, info)
    if major == 1:  # negative int
        val, pos = _cbor_read_length(data, pos, info)
        return -1 - val, pos
    if major in (2, 3):  # byte string / text string
        length, pos = _cbor_read_length(data, pos, info)
        if pos + length > len(data):
            raise _CborError("truncated string")
        chunk = data[pos:pos + length]
        pos += length
        return (chunk.decode("utf-8") if major == 3 else chunk), pos
    if major == 4:  # array
        length, pos = _cbor_read_length(data, pos, info)
        items = []
        for _ in range(length):
            item, pos = _cbor_decode_item(data, pos)
            items.append(item)
        return items, pos
    if major == 5:  # map
        length, pos = _cbor_read_length(data, pos, info)
        result: dict[Any, Any] = {}
        for _ in range(length):
            key, pos = _cbor_decode_item(data, pos)
            val, pos = _cbor_decode_item(data, pos)
            result[key] = val
        return result, pos
    raise _CborError(f"unsupported CBOR major type {major}")


def _decode_cbor_map(data: bytes) -> dict:
    """Decode CBOR bytes to a dict, preferring cbor2 if installed."""
    try:
        import cbor2  # type: ignore
        obj = cbor2.loads(data)
    except ImportError:
        obj, end = _cbor_decode_item(data, 0)
        if end != len(data):
            raise _CborError("trailing bytes after CBOR map")
    if not isinstance(obj, dict):
        raise _CborError("schema-2 payload is not a CBOR map")
    return obj


# ---------------------------------------------------------------------------
# Suffix parsing
# ---------------------------------------------------------------------------

def _to_bytes(calldata: Union[str, bytes]) -> bytes:
    if isinstance(calldata, bytes):
        return calldata
    h = calldata[2:] if calldata.startswith(("0x", "0X")) else calldata
    if len(h) % 2:
        return b""  # odd-length hex: not valid calldata
    try:
        return bytes.fromhex(h)
    except ValueError:
        return b""


def parse_attribution(calldata: Union[str, bytes]) -> Optional[Attribution]:
    """
    Parse an ERC-8021 attribution suffix from transaction calldata.

    Returns an Attribution, or None if no valid suffix is present.
    Never raises on malformed input — a settlement without attribution
    is normal, not an error.
    """
    data = _to_bytes(calldata)
    if len(data) < 17 or data[-16:] != ERC8021_MARKER:
        return None

    schema_id = data[-17]
    body = data[:-17]  # everything before schemaId

    if schema_id == SCHEMA_X402_BUILDER_CODE:
        return _parse_schema2(body, data)
    if schema_id == SCHEMA_GENERIC:
        return _parse_schema0(body, data)

    # Unknown schema: report presence without decoding, keep raw for evidence.
    return Attribution(schema_id=schema_id, raw_suffix_hex="0x" + data[-64:].hex())


def _parse_schema2(body: bytes, full: bytes) -> Optional[Attribution]:
    if len(body) < 2:
        return None
    cbor_len = int.from_bytes(body[-2:], "big")
    if cbor_len == 0 or cbor_len > len(body) - 2:
        return None
    cbor_data = body[-2 - cbor_len:-2]
    try:
        m = _decode_cbor_map(cbor_data)
    except (_CborError, Exception):
        return None

    def _clean(v: Any) -> Optional[str]:
        return v if isinstance(v, str) and CODE_PATTERN.match(v) else None

    s_raw = m.get("s")
    if isinstance(s_raw, str):
        service = [s_raw] if _clean(s_raw) else []
    elif isinstance(s_raw, list):
        service = [x for x in s_raw if isinstance(x, str) and _clean(x)]
    else:
        service = []

    attr = Attribution(
        schema_id=SCHEMA_X402_BUILDER_CODE,
        app_code=_clean(m.get("a")),
        facilitator_code=_clean(m.get("w")),
        service_codes=service,
        raw_suffix_hex="0x" + full[-(2 + cbor_len + 1 + 16 + 2):].hex(),
    )
    if not attr.all_codes():
        return None
    return attr


def _parse_schema0(body: bytes, full: bytes) -> Optional[Attribution]:
    if len(body) < 1:
        return None
    codes_len = body[-1]
    if codes_len == 0 or codes_len > len(body) - 1:
        return None
    try:
        codes_str = body[-1 - codes_len:-1].decode("ascii")
    except UnicodeDecodeError:
        return None
    codes = [c for c in codes_str.split(",") if CODE_PATTERN.match(c)]
    if not codes:
        return None
    return Attribution(
        schema_id=SCHEMA_GENERIC,
        codes=codes,
        raw_suffix_hex="0x" + full[-(codes_len + 1 + 1 + 16 + 2):].hex(),
    )
