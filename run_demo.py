"""Counterra demo on simulated data. Prefer: python3 counterra.py demo"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

SAMPLE_AGENTS = {
    "0x1111000000000000000000000000000000000aa1": "research-agent",
    "0x2222000000000000000000000000000000000aa2": "procurement-agent",
    "0x3333000000000000000000000000000000000aa3": "reporting-agent",
}
SAMPLE_PROVIDERS = {
    "0xA11ce00000000000000000000000000000000001": ("MarketFeed API",   "Market data"),
    "0xB0b0000000000000000000000000000000000002": ("GeoTiles API",     "Mapping data"),
    "0xC0ffee0000000000000000000000000000000003": ("LLM Inference Co", "AI inference"),
    "0xD00d000000000000000000000000000000000004": ("DocParse API",     "Document parsing"),
    "0xE55e000000000000000000000000000000000005": ("CreditSignals",    "Risk data"),
    "0xF00d000000000000000000000000000000000006": ("RenderFarm GPU",   "Compute"),
    "0xAB1e000000000000000000000000000000000007": ("NewsWire Pro",     "Market data"),
}

if __name__ == "__main__":
    import subprocess
    subprocess.run([sys.executable, os.path.join(os.path.dirname(__file__), "counterra.py"), "demo"])
