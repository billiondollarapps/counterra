"""
Solana collector for Counterra.

x402 on Solana settles as SPL-USDC token transfers submitted by
facilitator wallets (Coinbase runs several; PayAI is Solana-native).
Strategy mirrors the Base adapter:
  1. getSignaturesForAddress(facilitator) - newest-first settlement list
  2. getTransaction(signature, jsonParsed) - readable payment contents
  3. extract USDC transfer: payer owner -> payee owner -> amount
Token-account resolution: Solana transfers name token ACCOUNTS
(passbooks), not wallets. We map token accounts to owner wallets via
the transaction's own pre/postTokenBalances, so events attribute to
the actual agent and seller.

Non-custodial: read-only JSON-RPC against public endpoints.
"""
from datetime import datetime, timezone
import os
import time

from counterralib.ingest import PaymentEvent


class SolanaChainAdapter:
    def __init__(self, cfg, session=None, throttle=0.5, quiet=False):
        import requests
        sc = cfg["solana"]
        self.rpc = os.environ.get("SOLANA_RPC_URL", sc["rpc"])
        self.mint = sc["usdc_mint"]
        self.decimals = int(sc.get("usdc_decimals", 6))
        self.facilitators = list(sc.get("facilitators", []))
        self.session = session or requests.Session()
        self.throttle = throttle
        self.quiet = quiet
        self._id = 0

    def _say(self, msg):
        if not self.quiet:
            print(msg, flush=True)

    # ---------------- RPC letter-writing ----------------
    def _call(self, method, params):
        self._id += 1
        r = self.session.post(self.rpc, json={
            "jsonrpc": "2.0", "id": self._id,
            "method": method, "params": params}, timeout=30)
        r.raise_for_status()
        data = r.json()
        time.sleep(self.throttle)
        if "error" in data:
            raise RuntimeError(f"RPC error on {method}: {data['error']}")
        return data.get("result")

    def _signatures(self, address, limit):
        return self._call("getSignaturesForAddress",
                          [address, {"limit": limit}]) or []

    def _transaction(self, sig):
        return self._call("getTransaction", [sig, {
            "encoding": "jsonParsed",
            "maxSupportedTransactionVersion": 0}])

    # ---------------- decoding ----------------
    def _decode_tx(self, tx, ts_unix):
        """Extract USDC transfers as PaymentEvents from a parsed tx."""
        if not tx or (tx.get("meta") or {}).get("err") is not None:
            return []
        meta = tx["meta"]
        msg = tx["transaction"]["message"]
        keys = [k["pubkey"] if isinstance(k, dict) else k
                for k in msg.get("accountKeys", [])]

        # token-account passbook -> (mint, owner) map from balance snapshots
        acct = {}
        for bal in (meta.get("preTokenBalances") or []) + (meta.get("postTokenBalances") or []):
            idx = bal.get("accountIndex")
            if idx is None or idx >= len(keys):
                continue
            acct[keys[idx]] = (bal.get("mint"), bal.get("owner"))

        instrs = list(msg.get("instructions", []))
        for inner in (meta.get("innerInstructions") or []):
            instrs.extend(inner.get("instructions", []))

        out = []
        for ins in instrs:
            if not isinstance(ins, dict) or ins.get("program") != "spl-token":
                continue
            parsed = ins.get("parsed") or {}
            if parsed.get("type") not in ("transferChecked", "transfer"):
                continue
            info = parsed.get("info", {})
            dest = info.get("destination", "")
            mint = info.get("mint") or (acct.get(dest, (None, None))[0])
            if mint != self.mint:
                continue
            if "tokenAmount" in info:      # transferChecked
                ta = info["tokenAmount"]
                amount = int(ta["amount"]) / 10 ** int(ta.get("decimals", self.decimals))
            else:                          # plain transfer (raw base units)
                amount = int(info.get("amount", "0")) / 10 ** self.decimals
            payer = info.get("authority") or info.get("multisigAuthority") or ""
            payee = acct.get(dest, (None, dest))[1] or dest
            out.append(PaymentEvent(
                tx_hash=(tx.get("transaction", {}).get("signatures") or ["?"])[0],
                ts=datetime.fromtimestamp(ts_unix, tz=timezone.utc),
                chain="solana",
                payer_wallet=payer,
                payee_wallet=payee,
                amount_usdc=amount,
                protocol="x402",
                memo="facilitator-settled",
            ))
        return out

    # ---------------- mode 1: facilitator sweep ----------------
    def fetch(self, limit=60):
        events, skipped = [], 0
        per_fac = max(5, limit // max(1, len(self.facilitators)))
        for fac in self.facilitators:
            try:
                sigs = self._signatures(fac, per_fac)
            except Exception as e:
                self._say(f"  ! signature listing failed for {fac[:8]}...: {e}")
                continue
            sigs = [s for s in sigs if s.get("err") is None and s.get("blockTime")]
            self._say(f"  {fac[:8]}...: {len(sigs)} settlements listed; decoding...")
            for i, s in enumerate(sigs, 1):
                try:
                    tx = self._transaction(s["signature"])
                    events.extend(self._decode_tx(tx, int(s["blockTime"])))
                except Exception:
                    skipped += 1
                if i % 10 == 0:
                    self._say(f"    ...{i}/{len(sigs)} checked, {len(events)} payments decoded")
        if skipped:
            self._say(f"  (skipped {skipped} settlements whose details failed to fetch)")
        events.sort(key=lambda e: e.ts)
        return events

    # ---------------- mode 2: track one payer wallet ----------------
    def fetch_wallet(self, wallet, limit=200):
        """A wallet's signature list includes every tx that references it,
        including facilitator-submitted payments it authorized."""
        sigs = [s for s in self._signatures(wallet, limit)
                if s.get("err") is None and s.get("blockTime")]
        self._say(f"  {wallet[:8]}...: {len(sigs)} transactions referencing wallet; decoding...")
        events = []
        for i, s in enumerate(sigs, 1):
            try:
                tx = self._transaction(s["signature"])
                events.extend(e for e in self._decode_tx(tx, int(s["blockTime"]))
                              if e.payer_wallet == wallet)
            except Exception:
                pass
            if i % 10 == 0:
                self._say(f"    ...{i}/{len(sigs)} checked, {len(events)} payments decoded")
        events.sort(key=lambda e: e.ts)
        return events
