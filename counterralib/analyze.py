"""
Analysis layer for Counterra — explain the books, never write them.

Architecture principle (from the strategic design): AI generates and explains;
the deterministic engine decides and records. This module reads the books
Counterra has already produced and surfaces *findings* — spend concentration,
anomalies, unattributed exposure, cadence shifts. It NEVER computes or alters a
journal entry, an amount, or a bookability decision. Those belong to ledger.py
and CAAP-1, are conformance-tested, and must stay reproducible.

Two layers, deliberately separated:

  findings()   pure, deterministic. Produces a structured list of facts about
               the books ("blockrun.ai is 74% of spend across 5 payers";
               "19% of value is unattributed"). No LLM, no network, testable,
               always available. This is the ground truth.

  narrate()    optional. Hands the findings to an LLM to phrase as prose for a
               human. If no API key is configured it falls back to a plain
               template. The LLM only ever sees and rephrases findings it is
               given — it is told, in the system prompt, that it must not invent
               numbers. Because the findings are computed deterministically, a
               hallucinated figure can be caught by comparing against them.

The value in an AI-saturated market is precisely that Counterra's numbers are
NOT AI-generated. The agent is the shell; the deterministic books are the core.
"""

from __future__ import annotations

import os
from collections import defaultdict
from typing import List, Optional


def _load_events(db_path=None, chain=None):
    path = db_path or os.path.join("out", "counterra.db")
    if not os.path.exists(path):
        return []
    try:
        from counterralib.store import EventStore
        store = EventStore(path)
        try:
            return store.all_events(chain)
        finally:
            store.close()
    except Exception:
        return []


def findings(events=None, registry=None, db_path=None, chain=None):
    """
    Deterministic facts about the books. Returns a list of finding dicts:
      {kind, severity, headline, detail, numbers}

    Pure function of the ledger — no LLM, no network. This is the ground truth
    the narration layer is only allowed to rephrase.
    """
    evs = events if events is not None else _load_events(db_path, chain)
    out = []
    if not evs:
        return out

    reg = {}
    if isinstance(registry, dict):
        reg = {w.lower(): v for w, v in registry.items()}
    elif isinstance(registry, list):
        reg = {p["wallet"].lower(): p for p in registry
               if isinstance(p, dict) and p.get("wallet")}

    total = sum(e.amount_usdc for e in evs)
    n = len(evs)

    # by seller
    by_payee = defaultdict(lambda: {"amt": 0.0, "n": 0, "payers": set()})
    unattributed = {"amt": 0.0, "n": 0, "wallets": set()}
    for e in evs:
        w = e.payee_wallet.lower()
        d = by_payee[w]
        d["amt"] += e.amount_usdc; d["n"] += 1; d["payers"].add(e.payer_wallet.lower())
        if w not in reg:
            unattributed["amt"] += e.amount_usdc
            unattributed["n"] += 1
            unattributed["wallets"].add(w)

    # concentration
    top_w, top = max(by_payee.items(), key=lambda kv: kv[1]["amt"])
    top_share = top["amt"] / total if total else 0
    top_label = (reg.get(top_w) or {}).get("label", top_w[:16] + "…")
    if top_share >= 0.4:
        out.append({
            "kind": "concentration", "severity": "info",
            "headline": f"{top_label} is {top_share*100:.0f}% of all agent spend",
            "detail": (f"${top['amt']:.4f} of ${total:.4f} across {len(top['payers'])} "
                       f"distinct paying agents. Spend is concentrated in one seller; "
                       f"a change in that relationship would move the whole ledger."),
            "numbers": {"share": round(top_share, 4), "amount": round(top["amt"], 6),
                        "payers": len(top["payers"])},
        })

    # unattributed exposure
    if unattributed["amt"] > 0:
        share = unattributed["amt"] / total if total else 0
        out.append({
            "kind": "unattributed", "severity": "action" if share > 0.1 else "info",
            "headline": f"{share*100:.0f}% of spend value can't be attributed to a named seller",
            "detail": (f"${unattributed['amt']:.4f} across {unattributed['n']} settlements to "
                       f"{len(unattributed['wallets'])} unidentified wallets. These land in the "
                       f"uncategorized expense account until classified. Run `classify` or "
                       f"`profile` to reduce this."),
            "numbers": {"share": round(share, 4), "amount": round(unattributed["amt"], 6),
                        "wallets": len(unattributed["wallets"])},
        })

    # anomalies: single payments far above the median
    amounts = sorted(e.amount_usdc for e in evs)
    median = amounts[len(amounts) // 2]
    big = [e for e in evs if median > 0 and e.amount_usdc >= median * 100 and e.amount_usdc >= 1.0]
    if big:
        big.sort(key=lambda e: -e.amount_usdc)
        b = big[0]
        blabel = (reg.get(b.payee_wallet.lower()) or {}).get("label", b.payee_wallet[:16] + "…")
        out.append({
            "kind": "anomaly", "severity": "review",
            "headline": f"{len(big)} payment(s) far above the typical amount — largest ${b.amount_usdc:.2f} to {blabel}",
            "detail": (f"Median settlement is ${median:.6f}; these are 100x+ that. Large single "
                       f"payments are individually reviewable — confirm each was authorized."),
            "numbers": {"count": len(big), "largest": round(b.amount_usdc, 6),
                        "median": round(median, 6)},
        })

    # breadth
    out.append({
        "kind": "summary", "severity": "info",
        "headline": f"{n} settlements, ${total:.4f}, {len(by_payee)} sellers",
        "detail": (f"Median payment ${median:.6f}. {len([1 for d in by_payee.values() if d['n']==1])} "
                   f"sellers seen only once (trial), "
                   f"{len([1 for d in by_payee.values() if d['n']>=10])} with 10+ settlements (embedded)."),
        "numbers": {"settlements": n, "total": round(total, 6), "sellers": len(by_payee),
                    "median": round(median, 6)},
    })
    return out


def _template_narration(finds):
    """Plain-language narration with no LLM — always available."""
    if not finds:
        return "No agent spend in the ledger yet — nothing to analyze."
    lines = ["Here's what stands out in your agent spend:\n"]
    order = {"action": 0, "review": 1, "concentration": 2, "info": 3}
    for f in sorted(finds, key=lambda f: order.get(f["severity"], 9)):
        tag = {"action": "[ACTION]", "review": "[REVIEW]"}.get(f["severity"], "")
        lines.append(f"- {tag} {f['headline']}. {f['detail']}")
    return "\n".join(lines)


def narrate(finds, use_llm=True, model="claude-sonnet-4-6"):
    """
    Turn deterministic findings into prose.

    With an ANTHROPIC_API_KEY set and use_llm=True, an LLM rephrases the findings
    for a human. The model is explicitly instructed it may ONLY use the numbers
    in the findings and must not invent figures — and since findings are
    deterministic, any invented number is detectable. Without a key, falls back
    to a template so the feature always works.
    """
    if not finds:
        return "No agent spend in the ledger yet — nothing to analyze."
    if not use_llm or not os.environ.get("ANTHROPIC_API_KEY"):
        return _template_narration(finds)
    try:
        import json
        import urllib.request
        system = ("You explain a company's AI-agent spending to a finance person. "
                  "You are given a JSON list of FINDINGS computed deterministically "
                  "from their books. Rephrase them into a clear, brief summary. "
                  "CRITICAL: use ONLY the numbers present in the findings. Do NOT "
                  "invent, estimate, or extrapolate any figure. Do not add findings "
                  "that aren't in the input. If you're unsure, say less.")
        payload = {
            "model": model, "max_tokens": 1000,
            "system": system,
            "messages": [{"role": "user",
                          "content": "Findings:\n" + json.dumps(finds, indent=2)}],
        }
        req = urllib.request.Request(
            "https://api.anthropic.com/v1/messages",
            data=json.dumps(payload).encode(),
            headers={"content-type": "application/json",
                     "x-api-key": os.environ["ANTHROPIC_API_KEY"],
                     "anthropic-version": "2023-06-01"})
        with urllib.request.urlopen(req, timeout=30) as r:
            data = json.loads(r.read())
        text = "".join(b.get("text", "") for b in data.get("content", [])
                       if b.get("type") == "text")
        return text.strip() or _template_narration(finds)
    except Exception:
        # Never fail the analysis because the LLM call failed — fall back.
        return _template_narration(finds)
