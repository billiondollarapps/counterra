"""
Live Base-chain adapter for Counterra.

Primary data source: Blockscout's free public API (no key required).
Legacy Etherscan V2 mode is retained for paid tiers / other chains
(auto-selected when config api_base contains "etherscan").

How x402 traffic is found: settlements are submitted on-chain BY
facilitator wallets (Coinbase runs ~40 on Base and rotates them).
We list each facilitator's newest transactions, keep the ones that
call USDC or a canonical x402 contract (Permit2 proxies, batch escrow,
Multicall3), and decode every USDC Transfer inside:
payer (agent) -> payee (seller) -> amount, typed by settlement path.

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
        # facilitator wallet -> census name ("Coinbase", "PayAI", ...)
        self.facilitator_names = {f: "" for f in self.facilitators}
        self.usdc = self.cfg["usdc_contract"].lower()
        # A settlement is any facilitator tx that calls USDC directly OR goes
        # through a canonical x402 contract (Permit2 proxies, batch escrow,
        # Multicall3). Filtering on `to == USDC` alone silently drops every
        # Permit2 / batched settlement - which is how Base ingestion read zero.
        from counterralib.facilitators import X402_CONTRACTS
        self.settlement_targets = {self.usdc} | set(X402_CONTRACTS)
        self.diag = {}
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
    # Public Blockscout throttles intermittently. A single 30s hang used to be
    # enough to push a sweep past the CI step timeout, killing the run and
    # discarding everything (including a successful Solana sweep). So: short
    # per-request timeout, bounded retries with backoff, and 429 handled
    # explicitly. Failing fast and retrying beats hanging.
    REQ_TIMEOUT = 12
    MAX_TRIES = 3

    def _request(self, url, params=None):
        last = None
        for attempt in range(self.MAX_TRIES):
            try:
                r = self.session.get(url, params=params, timeout=self.REQ_TIMEOUT)
                if r.status_code == 429:
                    time.sleep(1.5 * (attempt + 1))
                    last = RuntimeError("429 rate limited")
                    continue
                if 500 <= r.status_code < 600:
                    time.sleep(1.0 * (attempt + 1))
                    last = RuntimeError("HTTP %d" % r.status_code)
                    continue
                r.raise_for_status()
                data = r.json()
                time.sleep(self.throttle)
                return data
            except Exception as e:
                last = e
                if attempt < self.MAX_TRIES - 1:
                    time.sleep(0.8 * (attempt + 1))
        raise last if last else RuntimeError("request failed")

    def _get(self, params):
        p = dict(params)
        if self.is_etherscan:
            p["chainid"] = self.cfg["chain_id"]
            p["apikey"] = self.api_key
        return self._request(self.cfg["api_base"], params=p)

    def _get_v2(self, path):
        return self._request(self.v2_base + path)

    # ---------------- transaction listing ----------------
    def _list_txs(self, fac, want):
        """Newest-first USDC-bound txs sent by a facilitator wallet."""
        if self.is_etherscan:
            txs = self._get({"module": "account", "action": "txlist",
                             "address": fac, "page": 1, "offset": want,
                             "sort": "desc"}).get("result", [])
            if not isinstance(txs, list):
                return []
            return [{"hash": t["hash"], "timeStamp": t["timeStamp"],
                     "to": (t.get("to") or "").lower()}
                    for t in txs
                    if str(t.get("isError", "0")) == "0"
                    and (t.get("to") or "").lower() in self.settlement_targets]

        out, params = [], ""
        while len(out) < want:
            data = self._get_v2(f"/addresses/{fac}/transactions?filter=from{params}")
            items = data.get("items", [])
            if not items:
                break
            for it in items:
                to = ((it.get("to") or {}).get("hash") or "").lower()
                self.diag["listed"] = self.diag.get("listed", 0) + 1
                if to not in self.settlement_targets:
                    od = self.diag.setdefault("other_targets", {})
                    od[to or "(none)"] = od.get(to or "(none)", 0) + 1
                    continue
                if (it.get("status") or "ok") != "ok":
                    self.diag["failed_tx"] = self.diag.get("failed_tx", 0) + 1
                    continue
                ts = (it.get("timestamp") or "").replace("Z", "+00:00")
                try:
                    unix = int(datetime.fromisoformat(ts).timestamp())
                except Exception:
                    continue
                out.append({"hash": it["hash"], "timeStamp": str(unix), "to": to})
                if len(out) >= want:
                    break
            np = data.get("next_page_params")
            if not np:
                break
            params = "&" + "&".join(f"{k}={v}" for k, v in np.items())
        return out

    # ---------------- mode 1: facilitator sweep ----------------
    def use_census(self, census, schedule=None, per_run=None):
        """Sweep the facilitator census instead of config.yaml's short list.

        `per_run` caps how many wallets this run visits; the schedule picks
        the ones with the best expected yield (see facilitators.pick_wallets).
        """
        from counterralib.facilitators import live_wallets, pick_wallets
        live = live_wallets(census, "base")
        if not live:
            return False
        names = {w["address"]: w["name"] for w in live}
        order = [w["address"] for w in live]
        self.schedule = schedule if schedule is not None else {}
        if per_run:
            order = pick_wallets(order, self.schedule, per_run)
        self.facilitators = order
        self.facilitator_names = names
        return True

    def fetch(self, limit=150, budget_seconds=None):
        """Sweep facilitators for settlements.

        budget_seconds: soft wall-clock budget. When exceeded, the sweep stops
        early and returns what it has instead of running until CI kills the
        step. Because ingestion is a persistent accumulating ledger, a partial
        sweep that COMMITS is far more valuable than a complete sweep that gets
        killed and commits nothing. Defaults to COUNTERRA_SWEEP_BUDGET or 300s.
        """
        if budget_seconds is None:
            try:
                budget_seconds = float(os.environ.get("COUNTERRA_SWEEP_BUDGET", "300"))
            except ValueError:
                budget_seconds = 300.0
        started = time.time()

        def over_budget():
            return budget_seconds and (time.time() - started) > budget_seconds

        from counterralib.facilitators import record_visit
        schedule = getattr(self, "schedule", None)
        events, skipped = [], 0
        stopped_early = False
        per_fac = max(10, limit // max(1, len(self.facilitators)))
        for fac in self.facilitators:
            if over_budget():
                stopped_early = True
                break
            name = self.facilitator_names.get(fac, "")
            try:
                txs = self._list_txs(fac, per_fac)
            except Exception as e:
                self.diag["listing_errors"] = self.diag.get("listing_errors", 0) + 1
                if schedule is not None:
                    from counterralib.facilitators import record_error
                    record_error(schedule, fac, e)
                self._say(f"  ! listing failed for {fac[:12]}... ({name or 'facilitator'}): {e}")
                continue
            if schedule is not None:
                stamps = [datetime.fromtimestamp(int(t["timeStamp"]), tz=timezone.utc).isoformat()
                          for t in txs]
                record_visit(schedule, fac, len(txs),
                             max(stamps) if stamps else None,
                             min(stamps) if stamps else None)
            self._say(f"  {fac[:12]}... {name or ''}: {len(txs)} settlements listed; decoding...")
            for i, tx in enumerate(txs, 1):
                if over_budget():
                    stopped_early = True
                    break
                try:
                    evs = self._decode_tx_logs(tx["hash"], int(tx["timeStamp"]),
                                               via=tx.get("to", ""), facilitator=fac)
                    events.extend(evs)
                except Exception:
                    skipped += 1
                if i % 20 == 0:
                    self._say(f"    ...{i}/{len(txs)} checked, {len(events)} payments decoded")
            if stopped_early:
                break
        self.diag["decoded"] = len(events)
        self.diag["skipped"] = skipped
        if skipped:
            self._say(f"  (skipped {skipped} settlements whose logs failed to fetch)")
        if stopped_early:
            self._say(f"  (stopped early at {int(time.time() - started)}s time budget — "
                      f"returning {len(events)} payments; the ledger accumulates across runs)")
        self._say("  diagnostics: " + self.diagnostics_line())
        events.sort(key=lambda e: e.ts)
        return events

    def diagnostics_line(self):
        """One line that says WHY a sweep came back thin - so the next stall
        is diagnosable from the Actions log instead of silent."""
        d = self.diag
        other = d.get("other_targets") or {}
        top = sorted(other.items(), key=lambda kv: -kv[1])[:3]
        return ("listed {} txs, {} failed on-chain, {} listing errors, "
                "{} payments decoded, {} log fetches failed; "
                "non-settlement targets: {}").format(
            d.get("listed", 0), d.get("failed_tx", 0), d.get("listing_errors", 0),
            d.get("decoded", 0), d.get("skipped", 0),
            ", ".join(f"{k[:10]}…x{v}" for k, v in top) or "none")

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

    def _decode_tx_logs(self, tx_hash, ts_unix, via="", facilitator=""):
        """Every USDC Transfer in a settlement tx, typed by how it settled.

        memo carries the leg type, which booking depends on:
          x402:exact          direct EIP-3009 payment (tx calls USDC)
          x402:permit2        payment routed through an x402 Permit2 proxy
          x402:multicall      payment inside a facilitator Multicall3 batch
          x402:batch-deposit  payer -> x402 batch escrow (prepaid, NOT expense)
          x402:batch-payout   escrow -> seller (settle) or payer (refund)
        Transfers sent BY the facilitator itself (gas/fee sweeps) are skipped.
        """
        from counterralib.facilitators import (X402_CONTRACTS, BATCH_ESCROW,
                                               BATCH_COLLECTORS)
        if self.is_etherscan:
            rec = self._get({"module": "proxy",
                             "action": "eth_getTransactionReceipt",
                             "txhash": tx_hash}).get("result") or {}
            logs = rec.get("logs", [])
        else:
            logs = self._get_v2(f"/transactions/{tx_hash}/logs").get("items", [])
        via = (via or "").lower()
        fac = (facilitator or "").lower()
        fac_name = self.facilitator_names.get(fac, "") if fac else ""
        escrow_side = BATCH_ESCROW | BATCH_COLLECTORS
        out = []
        for log in logs:
            addr = log.get("address")
            if isinstance(addr, dict):            # Blockscout v2 shape
                addr = addr.get("hash", "")
            if (addr or "").lower() != self.usdc:
                continue
            topics = [t for t in (log.get("topics") or []) if t]
            if len(topics) < 3 or topics[0].lower() != TRANSFER_TOPIC:
                continue
            payer = ("0x" + topics[1][-40:]).lower()
            payee = ("0x" + topics[2][-40:]).lower()
            if fac and payer == fac:
                continue                          # facilitator's own money
            if payee in BATCH_COLLECTORS and payer not in escrow_side:
                leg = "batch-deposit"
            elif payee in BATCH_ESCROW and payer in BATCH_COLLECTORS:
                continue                          # collector -> escrow hop
            elif payee in BATCH_ESCROW:
                leg = "batch-deposit"
            elif payer in escrow_side:
                leg = "batch-payout"
            elif via == self.usdc or not via:
                leg = "exact"
            else:
                kind = (X402_CONTRACTS.get(via) or ("", ""))[1]
                leg = "permit2" if kind == "permit2-proxy" else (
                      "multicall" if kind == "multicall" else "routed")
            amount = int(log.get("data") or "0x0", 16) / 10 ** self.cfg["usdc_decimals"]
            out.append(PaymentEvent(
                tx_hash=tx_hash,
                ts=datetime.fromtimestamp(ts_unix, tz=timezone.utc),
                chain=self.cfg["name"],
                payer_wallet=payer,
                payee_wallet=payee,
                amount_usdc=amount,
                protocol="x402",
                memo="x402:" + leg,
                facilitator=fac_name,
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