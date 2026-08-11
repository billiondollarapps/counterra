"""
Tests for CAAP-1 §6.1 — external document binding.

The properties that make this addition safe to put in a public standard:
  1. It is OPTIONAL — omitting it leaves every existing output unchanged.
  2. It is FORMAT-AGNOSTIC — a UBL e-invoice, a US PDF invoice and an Indian
     GST invoice all bind identically. The spec never parses the document.
  3. A missing hash is REJECTED, not silently dropped — the hash IS the audit
     binding, so an entry without one binds nothing.
  4. It does NOT affect bookability — a document reference is evidence, not a
     validity verdict. Jurisdiction-specific validation stays consumer-side and
     arrives through the existing failure_code taxonomy.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from counterralib.receipts import ReceiptError, receipt_to_journal

RECEIPT = {
    "scheme": "x402-receipts/v0",
    "payment": {"asset": "USDC", "amount": "2500000", "tx_hash": "0xabc",
                "payer": "0x" + "1" * 40, "payee": "0x" + "2" * 40,
                "settled_ts": "2026-08-01T10:00:00Z"},
    "request": {"method": "POST", "payment_requirements_sha256": "req123"},
    "response": {"status": 200, "latency_ms": 120, "body_sha256": "body123"},
    "goods": {"kind": "api-response", "description": "inference call"},
    "delivery": {"status": "delivered"},
}


def test_omitting_documents_changes_nothing():
    out = receipt_to_journal(RECEIPT)
    assert out["external_documents"] == []
    assert out["bookable"] is True
    assert out["amount_usd"] == 2.5
    print("omitting external_documents unchanged OK")


def test_invoice_hash_is_bound():
    out = receipt_to_journal(RECEIPT, external_documents=[
        {"kind": "invoice", "hash": "sha256:9f2b"}])
    assert out["external_documents"] == [
        {"kind": "invoice", "hash": "sha256:9f2b"}], out["external_documents"]
    # the receipt's own audit hashes are untouched
    assert out["body_sha256"] == "body123"
    assert out["payment_requirements_sha256"] == "req123"
    assert out["tx_hash"] == "0xabc"
    print("invoice hash bound alongside receipt hashes OK")


def test_format_agnostic_across_jurisdictions():
    # EN 16931 UBL, a US PDF, and an Indian GST invoice bind identically.
    docs = [{"kind": "invoice", "hash": "h-ubl"},
            {"kind": "invoice", "hash": "h-pdf"},
            {"kind": "gst-invoice", "hash": "h-irn"},
            {"kind": "credit-note", "hash": "h-cn"}]
    out = receipt_to_journal(RECEIPT, external_documents=docs)
    assert len(out["external_documents"]) == 4
    assert [d["kind"] for d in out["external_documents"]] == [
        "invoice", "invoice", "gst-invoice", "credit-note"]
    print("format/jurisdiction-agnostic binding OK")


def test_missing_hash_rejected():
    for bad in ([{"kind": "invoice"}], [{"kind": "invoice", "hash": ""}],
                [{"kind": "invoice", "hash": "   "}]):
        try:
            receipt_to_journal(RECEIPT, external_documents=bad)
            assert False, "should have raised: " + repr(bad)
        except ReceiptError as e:
            assert "hash" in str(e)
    print("entry without a hash rejected OK")


def test_non_dict_entry_rejected():
    try:
        receipt_to_journal(RECEIPT, external_documents=["sha256:abc"])
        assert False, "should have raised"
    except ReceiptError:
        pass
    print("non-object entry rejected OK")


def test_kind_defaults_and_hash_alg_only_when_unusual():
    out = receipt_to_journal(RECEIPT, external_documents=[
        {"hash": "abc"},
        {"hash": "def", "hash_alg": "sha256"},
        {"hash": "ghi", "hash_alg": "sha3-256"}])
    d = out["external_documents"]
    assert d[0]["kind"] == "document"
    assert "hash_alg" not in d[0]
    assert "hash_alg" not in d[1]          # sha256 is the default, not recorded
    assert d[2]["hash_alg"] == "sha3-256"  # non-default recorded
    print("kind default + hash_alg recorded only when non-default OK")


def test_document_does_not_affect_bookability():
    # A document reference is evidence, not a validity verdict. Attaching one
    # must not make an unbookable entry bookable, nor vice versa.
    ok = receipt_to_journal(RECEIPT, external_documents=[{"hash": "x"}])
    assert ok["bookable"] is True

    failed = dict(RECEIPT, delivery={"status": "failed"})
    out = receipt_to_journal(failed, external_documents=[{"hash": "x"}])
    assert out["bookable"] is False
    assert out["exception_code"] == "delivery_failed"
    assert out["external_documents"] == [{"kind": "document", "hash": "x"}]
    print("document reference does not affect bookability OK")


def test_jurisdictional_failure_routes_through_existing_taxonomy():
    # An EN 16931 validation failure is consumer-side: the caller validates and
    # passes an existing code. CAAP-1 gains no jurisdiction-specific code.
    out = receipt_to_journal(
        RECEIPT, failure_code="receipt_invalid",
        external_documents=[{"kind": "invoice", "hash": "h"}])
    assert out["bookable"] is False
    assert out["exception_code"] == "receipt_invalid"
    assert out["exception_actor"]              # who-must-act still resolves
    print("jurisdictional failure routes via existing taxonomy OK")


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
    print("\nALL EXTERNAL-DOCUMENT TESTS PASSED")
