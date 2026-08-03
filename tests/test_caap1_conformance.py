"""
CAAP-1 conformance suite.

Runs every golden vector in spec/caap1_vectors.json through the reference
implementation (counterralib.receipts.receipt_to_journal) and asserts the
normative output fields match. This is what makes "CAAP-1 conformant" a
checkable claim rather than a marketing phrase: any implementation — ours or a
third party's — is conformant iff it passes this suite.

The vectors were generated FROM the reference implementation, so this suite
also guards against the spec and the code silently drifting apart: change the
booking logic without regenerating vectors and this fails.
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from counterralib.receipts import receipt_to_journal

VECTORS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                       "spec", "caap1_vectors.json")

NORMATIVE_FIELDS = ["debit_account", "credit_account", "amount_usd",
                    "category", "bookable", "exception_reason", "date"]


def test_vectors_file_exists_and_wellformed():
    assert os.path.exists(VECTORS), "spec/caap1_vectors.json missing"
    data = json.load(open(VECTORS))
    assert data["spec"] == "CAAP-1"
    assert data["vectors"], "no vectors defined"
    print(f"vectors file OK ({len(data['vectors'])} vectors)")


def test_reference_implementation_is_conformant():
    data = json.load(open(VECTORS))
    failures = []
    for v in data["vectors"]:
        inp = v["input"]
        got = receipt_to_journal(inp["receipt"],
                                 registry=inp.get("registry"),
                                 expense_accounts=inp.get("expense_accounts"),
                                 verified=inp.get("verified"),
                                 failure_code=inp.get("failure_code"))
        for f in NORMATIVE_FIELDS:
            if got.get(f) != v["expect"].get(f):
                failures.append(f"{v['name']}.{f}: expected {v['expect'].get(f)!r}, "
                                f"got {got.get(f)!r}")
    assert not failures, "CAAP-1 NON-CONFORMANCE:\n  " + "\n  ".join(failures)
    print(f"reference implementation conformant on all {len(data['vectors'])} vectors")


def test_every_normative_field_present_in_vectors():
    data = json.load(open(VECTORS))
    for v in data["vectors"]:
        for f in NORMATIVE_FIELDS:
            assert f in v["expect"], f"vector {v['name']} missing normative field {f}"
    print("all vectors specify every normative field OK")


def test_both_bookable_and_exception_cases_covered():
    """A conformance suite that only tests happy paths proves little."""
    data = json.load(open(VECTORS))
    bookable = [v for v in data["vectors"] if v["expect"]["bookable"]]
    exceptions = [v for v in data["vectors"] if not v["expect"]["bookable"]]
    assert bookable, "no bookable vectors"
    assert exceptions, "no exception vectors"
    # the three defined exception conditions must each appear
    reasons = " ".join(v["expect"]["exception_reason"] or "" for v in exceptions)
    assert "failed" in reasons
    assert "partial" in reasons
    assert "status" in reasons  # http error
    print(f"coverage OK ({len(bookable)} bookable, {len(exceptions)} exception vectors)")


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
    print("\nCAAP-1 CONFORMANCE: PASS")
