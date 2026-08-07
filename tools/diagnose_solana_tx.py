"""
Diagnose what a Solana "payment" actually is.

Dumps a transaction's real anatomy - programs invoked, instruction types,
how many USDC transfers it contains, memos, signers, fee payer - so we can
see whether the collector booked a genuine x402 settlement or swept in a
swap, bridge, or unrelated transfer.

Usage:
    python3 diagnose_solana_tx.py                 # the 2 largest in the ledger
    python3 diagnose_solana_tx.py <signature>     # a specific tx
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import requests
import yaml

# Program IDs that mean "this is DeFi, not a service payment"
DEFI = {
    "JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4": "Jupiter aggregator",
    "JUP4Fb2cqiRUcaTHdrPC8h2gNsA2ETXiPDD33WcGuJB": "Jupiter v4",
    "675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8": "Raydium AMM",
    "CAMMCzo5YL8w4VFF8KVHrK22GGUsp5VTaW7grrKgrWqK": "Raydium CLMM",
    "whirLbMiicVdio4qvUfM5KAg6Ct8VwpYzGff3uctyCc": "Orca Whirlpool",
    "9WzDXwBbmkg8ZTbNMqUxvQRAyrZzDsGYdLVL9zYtAWWM": "Orca v1",
    "worm2ZoG2kUd4vFXhvjh93UUH596ayRfgQ2MgjNMTth": "Wormhole bridge",
    "PhoeNiXZ8ByJGLkxNfZRnkUfjvmuYqLR89jjFHGqdXY": "Phoenix DEX",
    "srmqPvymJeFKQ4zGQed1GFppgkRHL9kaELCbyksJtPX": "Serum DEX",
}
MEMO = ("MemoSq4gqABAXKb96qnH8TysNcWxMyWCqXgDLGmfcHr",
        "Memo1UhkJRfHyvLMcVucJwxXeuD728EqVDDwQDxFMNo")
TOKEN = ("TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",
         "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb")


def rpc(url, method, params):
    r = requests.post(url, json={"jsonrpc": "2.0", "id": 1,
                                 "method": method, "params": params},
                      timeout=40)
    r.raise_for_status()
    d = r.json()
    if "error" in d:
        raise RuntimeError(d["error"])
    return d.get("result")


def program_of(ins, keys):
    p = ins.get("programId")
    if p:
        return p
    i = ins.get("programIdIndex")
    return keys[i] if i is not None and i < len(keys) else "?"


def diagnose(sig, url, mint):
    print("=" * 72)
    print("TX", sig)
    tx = rpc(url, "getTransaction",
             [sig, {"encoding": "jsonParsed",
                    "maxSupportedTransactionVersion": 0}])
    if not tx:
        print("  not found (may be older than the RPC's history window)")
        return
    msg = tx["transaction"]["message"]
    meta = tx.get("meta") or {}
    keys = [k["pubkey"] if isinstance(k, dict) else k
            for k in msg.get("accountKeys", [])]
    signers = [k["pubkey"] for k in msg.get("accountKeys", [])
               if isinstance(k, dict) and k.get("signer")]

    print("  fee payer :", keys[0] if keys else "?")
    print("  signers   :", ", ".join(s[:12] + "..." for s in signers) or "?")
    print("  accounts  :", len(keys))

    top = list(msg.get("instructions", []))
    inner = []
    for g in (meta.get("innerInstructions") or []):
        inner.extend(g.get("instructions", []))

    progs = {}
    for ins in top + inner:
        p = program_of(ins, keys)
        progs[p] = progs.get(p, 0) + 1

    print("  programs invoked:")
    defi_found = []
    for p, n in sorted(progs.items(), key=lambda kv: -kv[1]):
        tag = ""
        if p in DEFI:
            tag = "  <-- " + DEFI[p] + "  *** NOT A SERVICE PAYMENT ***"
            defi_found.append(DEFI[p])
        elif p in MEMO:
            tag = "  <-- memo"
        elif p in TOKEN:
            tag = "  <-- SPL token"
        print("    %-46s x%-3d%s" % (p, n, tag))

    # memo contents
    memos = []
    for ins in top + inner:
        if program_of(ins, keys) in MEMO:
            pr = ins.get("parsed")
            memos.append(pr if isinstance(pr, str) else json.dumps(pr))
    print("  memo(s)   :", memos if memos else "none")

    # USDC transfers this tx contains
    transfers = []
    for ins in top + inner:
        if ins.get("program") != "spl-token":
            continue
        pr = ins.get("parsed") or {}
        if pr.get("type") not in ("transfer", "transferChecked"):
            continue
        info = pr.get("info", {})
        if "tokenAmount" in info:
            ta = info["tokenAmount"]
            amt = int(ta["amount"]) / 10 ** int(ta.get("decimals", 6))
            m = info.get("mint")
        else:
            amt = int(info.get("amount", "0")) / 10 ** 6
            m = None
        transfers.append((amt, m, info.get("authority", "?")))
    print("  USDC-ish transfers in this tx: %d" % len(transfers))
    for amt, m, auth in transfers:
        flag = "" if (m is None or m == mint) else "  (different mint)"
        print("     $%-12.6f auth %s...%s" % (amt, str(auth)[:12], flag))

    print("\n  VERDICT:")
    if defi_found:
        print("    DeFi programs present (%s) - these USDC movements are swap/"
              "bridge legs," % ", ".join(sorted(set(defi_found))))
        print("    NOT x402 service payments. The collector should drop them.")
    elif len(transfers) > 2:
        print("    %d USDC transfers in one transaction - looks like routing or"
              % len(transfers))
        print("    a batch, not a single service payment. Needs review.")
    elif memos:
        print("    Plain token transfer with a memo - consistent with an x402")
        print("    settlement. Probably genuine.")
    else:
        print("    Plain token transfer, no memo, no DeFi programs - ambiguous.")
        print("    Could be a genuine settlement or an ordinary USDC transfer")
        print("    that happened to reference a facilitator account.")
    print()


def main():
    cfg = yaml.safe_load(open("config.yaml"))
    url = os.environ.get("SOLANA_RPC_URL", cfg["solana"]["rpc"])
    mint = cfg["solana"]["usdc_mint"]

    if len(sys.argv) > 1:
        sigs = sys.argv[1:]
    else:
        from counterralib.store import EventStore
        st = EventStore()
        evs = st.all_events("solana")
        st.close()
        big = sorted(evs, key=lambda e: -e.amount_usdc)[:2]
        sigs = [e.tx_hash for e in big]
        print("Diagnosing the 2 largest Solana 'payments' in the ledger:\n")

    for s in sigs:
        try:
            diagnose(s, url, mint)
        except Exception as e:
            print("  ERROR on %s: %s\n" % (s[:20], e))


if __name__ == "__main__":
    main()
