"""
Base x402 collector that reads the chain directly over JSON-RPC.

Why: from 2026-10-01 Blockscout removed Base from its free tier, and every
keyless explorer route to Base data went with it. A plain Base RPC endpoint
(the public https://mainnet.base.org, or any free Alchemy / QuickNode /
Ankr URL in BASE_RPC_URL) needs no explorer at all.

It is also a better method, not just a replacement. The old sweep asked
"what did these N facilitator wallets send?" - so it only ever saw the
facilitators it knew about. This one asks the chain for the x402 settlement
SIGNATURES themselves, over a block range:

  1. USDC `AuthorizationUsed`  - fires on every EIP-3009 transferWithAuthorization,
                                 the x402 `exact` scheme
  2. any log from the x402 Permit2 proxies (Settled / SettledWithPermit)
  3. any log from x402BatchSettlement + its deposit collectors

  -> the set of settlement tx hashes in the range
  -> eth_getTransactionReceipt for each: sender (the facilitator), target,
     and every USDC Transfer inside
  -> the same typed PaymentEvents the Blockscout adapter produced
     (shared decoder: live.decode_usdc_logs)

EIP-3009 is used outside x402 too, so path 1 keeps a tx only when its sender
is a census facilitator. Paths 2 and 3 are x402 by construction and are kept
whoever submitted them (the sender is still recorded when it is known).

Progress is a block watermark in out/sweep_schedule.json, so each run picks
up where the last stopped. If a run hits its budget it saves the block it
reached; nothing is skipped silently. Python 3.8-safe.
"""

from __future__ import annotations

import os
import time
from typing import Dict, List

USDC_BASE = "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913"
AUTHORIZATION_USED = "0x98de503528ee59b575ef0c0a2576a82497bfc029a5685b209e9ec333479b10a5"
PERMIT2_PROXIES = ["0x402085c248eea27d92e8b30b2c58ed07f9e20001",
                   "0x4020a4f3b7b90cca423b9fabcc0ce57c6c240002"]
BATCH_CONTRACTS = ["0x4020074e9df2ce1dee5a9c1b5c3f541d02a10003",
                   "0x4020806089470a89826cb9fb1f4059150b550004",
                   "0x4020425faf3b746c082c2f942b4e5159887b0005"]
PUBLIC_RPC = "https://mainnet.base.org"
BLOCK_TIME = 2           # Base produces a block every 2 seconds, exactly
STATE_KEY = "_base_rpc"


class BaseRpcAdapter:
    def __init__(self, cfg, session=None, throttle=0.15, quiet=False,
                 rpc_url=None, chunk=None, state=None):
        import requests
        self.cfg = cfg["chain"]
        self.usdc = (self.cfg.get("usdc_contract") or USDC_BASE).lower()
        self.decimals = int(self.cfg.get("usdc_decimals", 6))
        self.rpc = rpc_url or os.environ.get("BASE_RPC_URL") or PUBLIC_RPC
        # getLogs block span per request; public endpoints reject wide spans
        self.chunk = int(chunk or os.environ.get("BASE_LOGS_CHUNK") or 500)
        self.session = session or requests.Session()
        self.throttle = throttle
        self.quiet = quiet
        self.state = state if state is not None else {}
        self.facilitators = set()
        self.facilitator_names: Dict[str, str] = {}
        self._id = 0
        self.batch_ok = None
        self.diag = {"blocks": 0, "candidates": 0, "kept": 0, "not_facilitator": 0,
                     "rpc_errors": 0, "decoded": 0}

    # ------------------------------------------------------------ plumbing --
    def _say(self, msg):
        if not self.quiet:
            print(msg, flush=True)

    def _call(self, method, params, tries=4):
        last = None
        for attempt in range(tries):
            self._id += 1
            try:
                r = self.session.post(self.rpc, json={"jsonrpc": "2.0", "id": self._id,
                                                      "method": method, "params": params},
                                      timeout=20)
                code = getattr(r, "status_code", 200)
                if code == 429 or 500 <= code < 600:
                    last = RuntimeError("HTTP %d%s" % (code, " rate limited" if code == 429 else ""))
                    # 429 = this endpoint is throttling us: back off hard
                    time.sleep((3.0 * 2 ** attempt) if code == 429 else 1.5 * (attempt + 1))
                    continue
                if code >= 400:
                    raise RuntimeError("HTTP %d: %s" % (code, (getattr(r, "text", "") or "")[:160]))
                data = r.json()
                if data.get("error"):
                    raise RuntimeError("RPC error on %s: %s" % (method, data["error"]))
                time.sleep(self.throttle)
                return data.get("result")
            except Exception as e:
                last = e
                msg = str(e).lower()
                # range too wide: the caller halves the span; don't burn retries
                if "range" in msg or "limit" in msg or "too many" in msg:
                    raise
                time.sleep(0.8 * (attempt + 1))
        self.diag["rpc_errors"] += 1
        raise last if last else RuntimeError("rpc failed")

    BATCH = 40

    def _receipts(self, hashes):
        """Receipts for many txs: JSON-RPC batches of 40, falling back to one
        call per tx if the endpoint does not accept batches."""
        out = {}
        if self.batch_ok is not False:
            for i in range(0, len(hashes), self.BATCH):
                part = hashes[i:i + self.BATCH]
                body = []
                for h in part:
                    self._id += 1
                    body.append({"jsonrpc": "2.0", "id": self._id,
                                 "method": "eth_getTransactionReceipt", "params": [h]})
                try:
                    r = self.session.post(self.rpc, json=body, timeout=30)
                    data = r.json() if getattr(r, "status_code", 200) < 400 else None
                    if not isinstance(data, list):
                        raise RuntimeError("batch not supported")
                    ids = {b["id"]: h for b, h in zip(body, part)}
                    for item in data:
                        if item.get("result") and item.get("id") in ids:
                            out[ids[item["id"]]] = item["result"]
                    self.batch_ok = True
                    time.sleep(self.throttle)
                except Exception:
                    if not self.batch_ok:
                        self.batch_ok = False      # never worked: go single
                        break
                    # worked before: transient - this part is retried singly below
            if self.batch_ok:
                missing = [h for h in hashes if h not in out]
            else:
                missing = list(hashes)
        else:
            missing = list(hashes)
        for h in missing:
            try:
                rec = self._call("eth_getTransactionReceipt", [h])
                if rec:
                    out[h] = rec
            except Exception as ex:
                self._say("  ! receipt failed {}: {}".format(h[:12], str(ex)[:120]))
        return out

    def use_census(self, census, schedule=None, per_run=None):
        """Facilitator set used to accept EIP-3009 txs as x402 (all of them:
        this method reads by block, so there is nothing to ration)."""
        from counterralib.facilitators import facilitator_index
        idx = {a: w for a, w in facilitator_index(census).items() if w["chain"] == "base"}
        if not idx:
            return False
        self.facilitators = set(idx)
        self.facilitator_names = {a: w["name"] for a, w in idx.items()}
        if schedule is not None:
            self.state = schedule
        return True

    def _logs(self, frm, to, address, topics=None):
        """eth_getLogs over [frm, to], halving the span if the node refuses."""
        out, start = [], frm
        span = to - frm + 1
        while start <= to:
            end = min(to, start + span - 1)
            flt = {"fromBlock": hex(start), "toBlock": hex(end), "address": address}
            if topics:
                flt["topics"] = topics
            try:
                out.extend(self._call("eth_getLogs", [flt]) or [])
                start = end + 1
            except Exception as e:
                # Halve only when the node says the RANGE is the problem. A 429
                # is throttling: shrinking the span just sends MORE requests.
                if span <= 10 or "429" in str(e) or "rate" in str(e).lower():
                    raise
                span = max(10, span // 2)
                self._say("    (node refused {} blocks: {} - retrying with {})".format(
                    end - start + 1, str(e)[:80], span))
        return out

    # --------------------------------------------------------------- sweep --
    def fetch(self, limit=150, budget_seconds=None, max_blocks=None):
        """Sweep from the saved block watermark to the chain head.

        limit: max settlement receipts to decode this run (each is one RPC
        call). max_blocks: cap on blocks scanned this run (default 10800 =
        6h of Base, the worker's cadence).
        """
        from counterralib.live import decode_usdc_logs
        if budget_seconds is None:
            try:
                budget_seconds = float(os.environ.get("COUNTERRA_SWEEP_BUDGET", "300"))
            except ValueError:
                budget_seconds = 300.0
        max_blocks = int(max_blocks or os.environ.get("BASE_MAX_BLOCKS") or 10800)
        started = time.time()

        def over_budget():
            return budget_seconds and (time.time() - started) > budget_seconds

        head = int(self._call("eth_blockNumber", []), 16)
        head_block = self._call("eth_getBlockByNumber", [hex(head), False]) or {}
        head_ts = int(head_block.get("timestamp", "0x0"), 16) or int(time.time())
        st = self.state.setdefault(STATE_KEY, {})
        start = int(st.get("next_block") or 0)
        if not start:
            # first run: look back one worker cycle rather than all of history
            start = head - min(max_blocks, 1800) + 1
        prior_state = dict(st)
        if head - start + 1 > max_blocks:
            gap_from, start_new = start, head - max_blocks + 1
            st["skipped_ranges"] = (st.get("skipped_ranges") or [])[-20:] + [
                [gap_from, start_new - 1]]
            self._say("  ! {} blocks behind - scanning the newest {}; skipped range "
                      "{}-{} recorded in the schedule".format(
                          head - start + 1, max_blocks, gap_from, start_new - 1))
            start = start_new
        self._say("  base rpc: blocks {:,}-{:,} ({:,} blocks) via {}".format(
            start, head, head - start + 1, self.rpc.split("/")[2]))

        events: List = []
        decoded_txs = 0
        reached = start - 1
        b = start
        stopped = ""
        while b <= head:
            if over_budget():
                stopped = "time budget"
                break
            e = min(head, b + self.chunk - 1)
            try:
                cand: Dict[str, str] = {}      # tx hash -> path
                for lg in self._logs(b, e, self.usdc, [AUTHORIZATION_USED]):
                    cand.setdefault(lg["transactionHash"], "eip3009")
                for lg in self._logs(b, e, PERMIT2_PROXIES):
                    cand[lg["transactionHash"]] = "permit2"
                for lg in self._logs(b, e, BATCH_CONTRACTS):
                    cand[lg["transactionHash"]] = "batch"
            except Exception as ex:
                self._say("  ! getLogs failed at blocks {}-{}: {}".format(b, e, str(ex)[:160]))
                stopped = "rpc error"
                break
            self.diag["candidates"] += len(cand)
            if decoded_txs + len(cand) > limit and decoded_txs:
                stopped = "receipt limit"
                break
            recs = self._receipts(list(cand))
            for h, path in cand.items():
                rec = recs.get(h)
                if not rec:
                    continue
                if rec.get("status") not in ("0x1", 1, None):
                    continue
                sender = (rec.get("from") or "").lower()
                if path == "eip3009" and sender not in self.facilitators:
                    self.diag["not_facilitator"] += 1
                    continue
                blk = int(rec.get("blockNumber", "0x0"), 16)
                ts = head_ts - (head - blk) * BLOCK_TIME
                evs = decode_usdc_logs(
                    rec.get("logs") or [], h, ts, usdc=self.usdc, decimals=self.decimals,
                    chain=self.cfg.get("name", "base"), via=rec.get("to") or "",
                    facilitator=sender,
                    facilitator_name=self.facilitator_names.get(sender, ""))
                if evs:
                    self.diag["kept"] += 1
                events.extend(evs)
                decoded_txs += 1
            reached = e
            self.diag["blocks"] += e - b + 1
            b = e + 1
        if reached < start:
            # Scanned nothing (e.g. the endpoint refused us). Leave the
            # watermark and skip log exactly as they were - otherwise every
            # failing run would record another "skipped" range.
            st.clear()
            st.update(prior_state)
            st["last_error_unix"] = int(time.time())
        else:
            st["next_block"] = reached + 1
        st["head_seen"] = head
        st["last_run_unix"] = int(time.time())
        self.diag["decoded"] = len(events)
        if stopped:
            self._say("  (stopped at block {:,}: {} - next run resumes there)".format(
                reached, stopped))
        self._say("  diagnostics: " + self.diagnostics_line())
        events.sort(key=lambda ev: ev.ts)
        return events

    def diagnostics_line(self):
        d = self.diag
        return ("rpc: {:,} blocks scanned, {} settlement txs found, {} kept "
                "({} EIP-3009 txs not from a known facilitator), {} payments decoded, "
                "{} rpc errors").format(d["blocks"], d["candidates"], d["kept"],
                                        d["not_facilitator"], d["decoded"], d["rpc_errors"])
