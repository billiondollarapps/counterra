"""
Persistent event store for Counterra — turns snapshot runs into a running ledger.

Design goal: each run remembers what it already saw, so the books accumulate
instead of resetting. A run stores newly-seen PaymentEvents, records a
high-water mark per (chain, source) so the next run can resume, and can hand
back the full accumulated history for the ledger pipeline.

Storage is local SQLite (a single file, zero infra, ships with Python) to keep
Counterra clone-and-run and non-custodial. All persistence is isolated behind
the EventStore class: a future hosted backend (e.g. Postgres/Supabase) only
needs to reimplement these same methods — the ingestion/accumulation logic in
continuous.py never touches SQL directly.

Idempotency: events are keyed by (tx_hash, payer_wallet, payee_wallet, amount)
so re-ingesting an overlapping window never double-counts. Re-running a sweep
is always safe.
"""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timezone
from typing import Iterable, List, Optional

from counterralib.ingest import PaymentEvent

DEFAULT_DB_PATH = os.path.join("out", "counterra.db")


def _event_key(e: PaymentEvent) -> str:
    """Stable identity for an event, for idempotent inserts."""
    return f"{e.chain}:{e.tx_hash}:{e.payer_wallet}:{e.payee_wallet}:{e.amount_usdc}"


def _event_from_row(d: dict) -> PaymentEvent:
    """Rebuild a PaymentEvent from a stored dict (inverse of to_dict)."""
    ts = d["ts"]
    ts = datetime.fromisoformat(ts) if isinstance(ts, str) else ts
    return PaymentEvent(
        tx_hash=d["tx_hash"],
        ts=ts,
        chain=d["chain"],
        payer_wallet=d["payer_wallet"],
        payee_wallet=d["payee_wallet"],
        amount_usdc=d["amount_usdc"],
        protocol=d.get("protocol", "x402"),
        memo=d.get("memo", ""),
        app_code=d.get("app_code", ""),
        facilitator_code=d.get("facilitator_code", ""),
        service_codes=d.get("service_codes", ""),
        attribution_evidence=d.get("attribution_evidence", ""),
    )


class EventStore:
    """
    A persistent, append-only store of PaymentEvents plus per-source state.

    Typical use:
        store = EventStore()                    # opens/creates out/counterra.db
        new = store.add_events(events)          # returns count of genuinely-new rows
        store.set_watermark("base", "sweep", latest_ts)
        all_events = store.all_events("base")   # full accumulated history
    """

    def __init__(self, db_path: str = DEFAULT_DB_PATH):
        self.db_path = db_path
        d = os.path.dirname(db_path)
        if d:
            os.makedirs(d, exist_ok=True)
        self.conn = sqlite3.connect(db_path)
        self.conn.row_factory = sqlite3.Row
        self._init_schema()

    def _init_schema(self):
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS events (
                event_key   TEXT PRIMARY KEY,
                chain       TEXT NOT NULL,
                tx_hash     TEXT NOT NULL,
                ts          TEXT NOT NULL,
                payer_wallet TEXT NOT NULL,
                payee_wallet TEXT NOT NULL,
                amount_usdc REAL NOT NULL,
                protocol    TEXT,
                memo        TEXT,
                app_code    TEXT,
                facilitator_code TEXT,
                service_codes    TEXT,
                attribution_evidence TEXT,
                first_seen  TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_events_chain_ts ON events(chain, ts);

            CREATE TABLE IF NOT EXISTS ingest_state (
                chain    TEXT NOT NULL,
                source   TEXT NOT NULL,
                watermark_ts TEXT,
                last_run_ts  TEXT,
                events_total INTEGER DEFAULT 0,
                PRIMARY KEY (chain, source)
            );
            """
        )
        self.conn.commit()

    # ---------------- event ingestion ----------------
    def add_events(self, events: Iterable[PaymentEvent]) -> int:
        """
        Insert events idempotently. Returns the number of genuinely-new rows
        (duplicates from overlapping windows are silently ignored).
        """
        now = datetime.now(timezone.utc).isoformat()
        new_count = 0
        cur = self.conn.cursor()
        for e in events:
            d = e.to_dict()
            key = _event_key(e)
            cur.execute("SELECT 1 FROM events WHERE event_key = ?", (key,))
            if cur.fetchone():
                continue
            cur.execute(
                """INSERT INTO events
                   (event_key, chain, tx_hash, ts, payer_wallet, payee_wallet,
                    amount_usdc, protocol, memo, app_code, facilitator_code,
                    service_codes, attribution_evidence, first_seen)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    key, d["chain"], d["tx_hash"], d["ts"],
                    d["payer_wallet"], d["payee_wallet"], d["amount_usdc"],
                    d.get("protocol", ""), d.get("memo", ""),
                    d.get("app_code", ""), d.get("facilitator_code", ""),
                    d.get("service_codes", ""), d.get("attribution_evidence", ""),
                    now,
                ),
            )
            new_count += 1
        self.conn.commit()
        return new_count

    # ---------------- retrieval ----------------
    def all_events(self, chain: Optional[str] = None) -> List[PaymentEvent]:
        """Full accumulated history, oldest-first. Optionally filter by chain."""
        if chain:
            rows = self.conn.execute(
                "SELECT * FROM events WHERE chain = ? ORDER BY ts ASC", (chain,)
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM events ORDER BY ts ASC"
            ).fetchall()
        return [_event_from_row(dict(r)) for r in rows]

    def event_count(self, chain: Optional[str] = None) -> int:
        if chain:
            r = self.conn.execute(
                "SELECT COUNT(*) AS n FROM events WHERE chain = ?", (chain,)
            ).fetchone()
        else:
            r = self.conn.execute("SELECT COUNT(*) AS n FROM events").fetchone()
        return r["n"]

    # ---------------- resume state (high-water mark) ----------------
    def get_watermark(self, chain: str, source: str) -> Optional[str]:
        """Latest event timestamp (ISO string) this source has ingested, or None."""
        r = self.conn.execute(
            "SELECT watermark_ts FROM ingest_state WHERE chain = ? AND source = ?",
            (chain, source),
        ).fetchone()
        return r["watermark_ts"] if r else None

    def set_watermark(self, chain: str, source: str, watermark_ts: Optional[str]):
        """Record how far this (chain, source) has ingested, and stamp the run."""
        now = datetime.now(timezone.utc).isoformat()
        total = self.event_count(chain)
        existing = self.get_watermark(chain, source)
        # Never move the watermark backwards.
        if existing and watermark_ts and watermark_ts < existing:
            watermark_ts = existing
        self.conn.execute(
            """INSERT INTO ingest_state (chain, source, watermark_ts, last_run_ts, events_total)
               VALUES (?,?,?,?,?)
               ON CONFLICT(chain, source) DO UPDATE SET
                 watermark_ts = excluded.watermark_ts,
                 last_run_ts  = excluded.last_run_ts,
                 events_total = excluded.events_total""",
            (chain, source, watermark_ts, now, total),
        )
        self.conn.commit()

    def state_summary(self) -> List[dict]:
        """All ingest sources and how far each has progressed (for status output)."""
        rows = self.conn.execute(
            "SELECT * FROM ingest_state ORDER BY chain, source"
        ).fetchall()
        return [dict(r) for r in rows]

    def close(self):
        self.conn.close()
