"""Live Base-chain adapter (Etherscan V2 / Blockscout compatible)."""
from datetime import datetime, timezone
import os
import time

from scribelib.ingest import PaymentEvent

TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"


class BaseChainAdapter:
    def __init__(self, cfg, api_key=None, session=None, throttle=0.35):
        import requests
        self.cfg = cfg["chain"]
        self.facilitators = [f.lower() for f in cfg.get("facilitators", [])]
        self.api_key = api_key or os.environ.get("ETHERSCAN_API_KEY", "")
        self.session = session or requests.Session()
        self.throttle = throttle
        self.is_etherscan = "etherscan" in self.cfg["api_base"]
        # Blockscout's modern REST API lives at /api/v2
        self.v2_base = self.cfg["api_base"].rstrip("/").rsplit("/api", 1)[0] + "/api/v2"

    def _get(self, params):
        p = dict(params)
        if self.is_etherscan:
            p["chainid"] = self.cfg["chain_id"]
            p["apikey"] = self.api_key
        r = self.session.get(self.cfg["api_base"], params=p, timeout=30)
        r.raise_for_status()
        data = r.json()
        time.sleep(self.throttle)
        return data

    def _get_v2(self, path):
        r = self.session.get(self.v2_base + path, timeout=30)
        r.raise_for_status()
        data = r.json()
        time.sleep(self.throttle)
        return data

    def _list_txs_v2(self, fac, want):
        txs, params = [], ""
        while len(txs) < want:
            data = self._get_v2(f"/addresses/{fac}/transactions?filter=from{params}")
            items = data.get("items", [])
            if not items:
                break
            for it in items:
                to = ((it.get("to") or {}).get("hash") or "").lower()
                if to != self.cfg["usdc_contract"].lower():
                    continue
                if (it.get("status") or "ok") != "ok":
                    continue
                ts = it.get("timestamp", "").replace("Z", "+00:00")
                try:
                    unix = int(datetime.fromisoformat(ts).timestamp())
                except Exception:
                    continue
                txs.append({"hash": it["hash"], "timeStamp": str(unix),
                            "to": to, "isError": "0"})
                if len(txs) >= want:
                    break
            np = data.get("next_page_params")
            if not np:
                break
            params = "&" + "&".join(f"{k}={v}" for k, v in np.items())
        return txs

    def fetch(self, limit=150):
        events, skipped = [], 0
        per_fac = max(10, limit // max(1, len(self.facilitators)))
        for fac in self.facilitators:
            try:
                if self.is_etherscan:
                    txs = self._get({"module": "account", "action": "txlist",
                                     "address": fac, "page": 1, "offset": per_fac,
                                     "sort": "desc"}).get("result", [])
                else:
                    txs = self._list_txs_v2(fac, per_fac)
            except Exception as e:
                print(f"  ! txlist failed for {fac[:12]}…: {e}")
                continue
            if not isinstance(txs, list):
                continue
            for tx in txs:
                if str(tx.get("isError", "0")) != "0":
                    continue
                if (tx.get("to") or "").lower() != self.cfg["usdc_contract"].lower():
                    continue
                try:
                    events.extend(self._decode_tx_logs(tx["hash"], int(tx["timeStamp"])))
                except Exception:
                    skipped += 1
        if skipped:
            print(f"  (skipped {skipped} settlements whose logs failed to fetch)")
        events.sort(key=lambda e: e.ts)
        return events

    def fetch_wallet(self, wallet, limit=500):
        rows = self._get({"module": "account", "action": "tokentx",
                          "address": wallet,
                          "contractaddress": self.cfg["usdc_contract"],
                          "page": 1, "offset": limit, "sort": "desc"}).get("result", [])
        events = []
        if not isinstance(rows, list):
            return events
        for r in rows:
            if r.get("from", "").lower() != wallet.lower():
                continue
            events.append(PaymentEvent(
                tx_hash=r["hash"],
                ts=datetime.fromtimestamp(int(r["timeStamp"]), tz=timezone.utc),
                chain=self.cfg["name"],
                payer_wallet=r["from"].lower(),
                payee_wallet=r["to"].lower(),
                amount_usdc=int(r["value"]) / 10 ** self.cfg["usdc_decimals"],
                protocol="x402",
                memo="tokentx",
            ))
        events.sort(key=lambda e: e.ts)
        return events

    # ---- log decoding: Blockscout v2 REST or Etherscan proxy ----
    def _decode_tx_logs(self, tx_hash, ts_unix):
        if self.is_etherscan:
            rec = self._get({"module": "proxy",
                             "action": "eth_getTransactionReceipt",
                             "txhash": tx_hash}).get("result") or {}
            logs = rec.get("logs", [])
        else:
            logs = self._get_v2(f"/transactions/{tx_hash}/logs").get("items", [])
        out = []
        for log in logs:
            addr = log.get("address")
            if isinstance(addr, dict):          # Blockscout v2 shape
                addr = addr.get("hash", "")
            if (addr or "").lower() != self.cfg["usdc_contract"].lower():
                continue
            topics = [t for t in (log.get("topics") or []) if t]
            if len(topics) < 3 or topics[0].lower() != TRANSFER_TOPIC:
                continue
            payer = "0x" + topics[1][-40:]
            payee = "0x" + topics[2][-40:]
            amount = int(log.get("data") or "0x0", 16) / 10 ** self.cfg["usdc_decimals"]
            out.append(PaymentEvent(
                tx_hash=tx_hash,
                ts=datetime.fromtimestamp(ts_unix, tz=timezone.utc),
                chain=self.cfg["name"],
                payer_wallet=payer.lower(),
                payee_wallet=payee.lower(),
                amount_usdc=amount,
                protocol="x402",
                memo="facilitator-settled",
            ))
        return out
