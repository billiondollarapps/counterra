"""
Live Base-chain adapter for Counterra.

Primary data source: Blockscout's free public API (no key required).
Legacy Etherscan V2 mode is retained for paid tiers / other chains
(auto-selected when config api_base contains "etherscan").

How x402 traffic is found: settlements are submitted on-chain BY
facilitator wallets (Coinbase runs ~40 on Base and rotates them).
We list each facilitator's newest transactions, keep the ones that
call the USDC contract, and decode the ERC-20 Transfer inside:
payer (agent) -> payee (seller) -> amount.

ERC-8021 attribution: settlements may carry a builder-code suffix in
their calldata (which app served the endpoint, which facilitator
settled, which client paid). When a settlement yields payment events,
we fetch its calldata once and attach any decoded attribution to
every event from that settlement.

`refresh_facilitators()` pulls the community-maintained registry
(x402scan repo) and rewrites config.yaml with the newest wallets -
field-learned feature: hardcoded facilitator lists go stale.
"""
from datetime import datetime, timezone
import os
import re
import time

from counterralib.ingest import PaymentEvent
from .erc8021 import parse_attribution

TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"


class BaseChainAdapter:
    def __init__(self, cfg, api_key=None, session=None, throttle=0.35, quiet=False):
        import requests
        self.cfg = cfg["chain"]
        self.facilitators = [f.lower() for f in cfg.get("facilitators", [])]
        self.api_key = api_key or os.environ.get("ETHERSCAN_API_KEY", "")
        self.session = session or requests.Session()
        self.throttle = throttle
        self.quiet = quiet
        self.is_etherscan = "etherscan" in self.cfg["api_base"]
        self.v2_base = self.cfg["api_base"].rstrip("/").rsplit("/api", 1)[0] + "/api/v2"

    def _say(self, msg):
        if not self.quiet:
            print(msg, flush=True)

    # ---------------- HTTP helpers ----------------
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

    # ---------------- transaction listing ----------------
    def _list_txs(self, fac, want):
        """Newest-first USDC-bound txs sent by a facilitator wallet."""
        if self.is_etherscan:
            txs = self._get({"module": "account", "action": "txlist",
                             "address": fac, "page": 1, "offset": want,
                             "sort": "desc"}).get("result", [])
            if not isinstance(txs, list):
                return []
            return [{"hash": t["hash"], "timeStamp": t["timeStamp"]}
                    for t in txs
                    if str(t.get("isError", "0")) == "0"
                    and (t.get("to") or "").lower() == self.cfg["usdc_contract"].lower()]

        out, params = [], ""
        while len(out) < want:
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
                ts = (it.get("timestamp") or "").replace("Z", "+00:00")
                try:
                    unix = int(datetime.fromisoformat(ts).timestamp())
                except Exception:
                    continue
                out.append({"hash": it["hash"], "timeStamp": str(unix)})
                if len(out) >= want:
                    break
            np = data.get("next_page_params")
            if not np:
                break
            params = "&" + "&".join(f"{k}={v}" for k, v in np.items())
        return out

    # ---------------- mode 1: facilitator sweep ----------------
    def fetch(self, limit=150):
        events, skipped = [], 0
        per_fac = max(10, limit // max(1, len(self.facilitators)))
        for fac in self.facilitators:
            try:
                txs = self._list_txs(fac, per_fac)
            except Exception as e:
                self._say(f"  ! listing failed for {fac[:12]}...: {e}")
                continue
            self._say(f"  {fac[:12]}...: {len(txs)} settlements listed; decoding...")
            for i, tx in enumerate(txs, 1):
                try:
                    events.extend(self._decode_tx_logs(tx["hash"], int(tx["timeStamp"])))
                except Exception:
                    skipped += 1
                if i % 20 == 0:
                    self._say(f"    ...{i}/{len(txs)} checked, {len(events)} payments decoded")
        if skipped:
            self._say(f"  (skipped {skipped} settlements whose logs failed to fetch)")
        events.sort(key=lambda e: e.ts)
        return events

    # ---------------- mode 2: track one payer wallet ----------------
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

    # ---------------- log decoding ----------------
    def _get_tx_input(self, tx_hash):
        """Fetch a transaction's calldata (where ERC-8021 suffixes live)."""
        try:
            if self.is_etherscan:
                rec = self._get({"module": "proxy",
                                 "action": "eth_getTransactionByHash",
                                 "txhash": tx_hash}).get("result") or {}
                return rec.get("input", "") or ""
            tx = self._get_v2(f"/transactions/{tx_hash}") or {}
            return tx.get("raw_input", "") or ""
        except Exception:
            return ""

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
            if isinstance(addr, dict):            # Blockscout v2 shape
                addr = addr.get("hash", "")
            if (addr or "").lower() != self.cfg["usdc_contract"].lower():
                continue
            topics = [t for t in (log.get("topics") or []) if t]
            if len(topics) < 3 or topics[0].lower() != TRANSFER_TOPIC:
                continue
            amount = int(log.get("data") or "0x0", 16) / 10 ** self.cfg["usdc_decimals"]
            out.append(PaymentEvent(
                tx_hash=tx_hash,
                ts=datetime.fromtimestamp(ts_unix, tz=timezone.utc),
                chain=self.cfg["name"],
                payer_wallet=("0x" + topics[1][-40:]).lower(),
                payee_wallet=("0x" + topics[2][-40:]).lower(),
                amount_usdc=amount,
                protocol="x402",
                memo="facilitator-settled",
            ))
        if out:
            attr = parse_attribution(self._get_tx_input(tx_hash))
            if attr:
                for ev in out:
                    ev.app_code = attr.app_code or ""
                    ev.facilitator_code = attr.facilitator_code or ""
                    ev.service_codes = ",".join(attr.service_codes)
                    ev.attribution_evidence = attr.raw_suffix_hex
        return out


# ---------------- facilitator registry refresh ----------------
def refresh_facilitators(config_path, keep=6, session=None):
    """Pull the x402scan community registry and rewrite the
    facilitators block in config.yaml with the newest wallets."""
    import requests
    import yaml
    cfg = yaml.safe_load(open(config_path))
    url = cfg.get("facilitator_registry")
    if not url:
        raise SystemExit("config.yaml has no facilitator_registry URL")
    s = session or requests.Session()
    src = s.get(url, timeout=30).text
    pairs = re.findall(
        r"address:\s*'(0x[0-9a-fA-F]{40})'.*?Date\('([\d-]+)'\)", src, re.S)
    if not pairs:
        raise SystemExit("registry format changed - no addresses found; "
                         "update the parser in counterralib/live.py")
    pairs.sort(key=lambda p: p[1], reverse=True)
    newest = pairs[:keep]

    raw = open(config_path).read()
    start = raw.index("facilitators:")
    tail_markers = ["\n# Map payer wallets", "\nagents:"]
    end = min(raw.index(m) for m in tail_markers if m in raw)
    block = "facilitators:\n"
    block += ("  # Newest Coinbase x402 facilitator wallets "
              "(auto-refreshed from x402scan registry)\n")
    for a, d in newest:
        block += f'  - "{a}"   # first tx {d}\n'
    block += "\n"
    open(config_path, "w").write(raw[:start] + block + raw[end:])
    print(f"config.yaml updated with {len(newest)} newest facilitator wallets "
          f"(of {len(pairs)} in registry):")
    for a, d in newest:
        print(f"  {a}  (first tx {d})")