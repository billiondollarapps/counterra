"""
Offline test for batch classification.
Fakes: a journal CSV with two unmapped sellers, a discovery catalog
knowing one of them. Verifies: identification, category suggestion,
registry append with dedup, unknown left honest.
Run: python3 tests/test_classify.py
"""
import sys, os, json, csv, subprocess, tempfile, shutil
HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

from counterralib.whois import identify, suggest_category

KNOWN = "So1anaSe11erKnownWa11etAbCdEfGhJkMnPqRsTu"
MYSTERY = "So1anaSe11erMysteryXyZaBcDeFgHjKmNpQrStUv"

class FakeResp:
    def __init__(self, payload): self.payload = payload
    def raise_for_status(self): pass
    def json(self): return self.payload

class FakeCatalog:
    def get(self, url, params=None, timeout=None):
        if (params or {}).get("offset", 0) == 0 and "coinbase" in url:
            return FakeResp({"items": [
                {"resource": {"url": "https://neuralforge.io/api/inference/run",
                              "description": "LLM inference"},
                 "accepts": [{"payTo": KNOWN}]}],
                "pagination": {"total": 1, "limit": 100}})
        return FakeResp({"items": [], "pagination": {"total": 0, "limit": 100}})

def main():
    # identify(): hit + category suggestion
    hit = identify(KNOWN, session=FakeCatalog())
    assert hit["label"] == "neuralforge.io", hit
    assert hit["category_suggestion"] == "AI inference", hit
    miss = identify(MYSTERY, session=FakeCatalog())
    assert miss["label"] is None

    # end-to-end classify --write in a sandbox copy of the repo files
    with tempfile.TemporaryDirectory() as td:
        for f in ["counterra.py", "config.yaml", "report.py", "run_demo.py"]:
            shutil.copy(os.path.join(HERE, f), td)
        shutil.copytree(os.path.join(HERE, "counterralib"), os.path.join(td, "counterralib"))
        os.makedirs(os.path.join(td, "docs")); os.makedirs(os.path.join(td, "out"))
        json.dump({"version": 1, "providers": []},
                  open(os.path.join(td, "docs", "providers.json"), "w"))
        with open(os.path.join(td, "out", "journal_entries.csv"), "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["period","debit_account","credit_account","amount_usd",
                        "provider","provider_wallet","category","settlements","memo"])
            w.writerow(["2026-07","6490","1085","1.50","x",KNOWN,"Uncategorized","3","m"])
            w.writerow(["2026-07","6490","1085","0.40","y",MYSTERY,"Uncategorized","1","m"])
        # monkeypatch requests.Session inside the subprocess via sitecustomize
        open(os.path.join(td, "sitecustomize.py"), "w").write(
            "import counterralib.whois as W\n"
            "import tests_fake\n"
            "W.identify.__defaults__ = (tests_fake.FakeCatalog(),)\n")
        shutil.copy(__file__, os.path.join(td, "tests_fake.py"))
        env = dict(os.environ, PYTHONPATH=td)
        r = subprocess.run([sys.executable, os.path.join(td, "counterra.py"),
                            "classify", "--write"], capture_output=True, text=True, env=env, cwd=td)
        out = r.stdout
        assert "1 identified, 1 remain unknown" in out, out + r.stderr
        reg = json.load(open(os.path.join(td, "docs", "providers.json")))
        assert len(reg["providers"]) == 1
        assert reg["providers"][0]["label"] == "neuralforge.io"
        assert reg["providers"][0]["category"] == "AI inference"

    print("ALL CLASSIFY TESTS PASSED")
    print("  identify: catalog hit + AI-inference suggestion; miss stays honest")
    print("  classify --write: 1 appended to registry, 1 left unknown, dedup path exercised")

if __name__ == "__main__":
    main()
