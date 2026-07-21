"""
Continuous ingestion for Counterra — snapshot runs become a running ledger.

This is the thin orchestration layer between the chain adapters (which fetch
events) and the ledger pipeline (which turns events into books). It:

  1. fetches this run's events from an adapter (unchanged snapshot behaviour),
  2. stores only the genuinely-new ones in the persistent EventStore,
  3. advances a per-source high-water mark so progress is recorded,
  4. hands back the FULL accumulated history for the ledger to render.

The result: run it Monday, run it Wednesday, and Wednesday's books include
Monday's spend plus everything new — instead of re-deriving a fresh snapshot
each time. Re-running an overlapping window is always safe (idempotent store).

No SQL lives here — all persistence is delegated to EventStore, so a future
hosted backend swaps the store without touching this accumulation logic.
"""

from __future__ import annotations

from typing import List, Optional

from counterralib.store import EventStore


def ingest_run(events, chain: str, source: str,
               store: Optional[EventStore] = None,
               db_path: Optional[str] = None):
    """
    Persist a run's events and return (all_events, stats).

    Args:
        events:  freshly-fetched PaymentEvents from an adapter this run.
        chain:   "base" | "solana" (namespaces the store + watermark).
        source:  "sweep" | "wallet:0x..." (distinguishes ingest streams).
        store:   an existing EventStore (tests inject one); else opened from db_path.
        db_path: optional override for the SQLite file location.

    Returns:
        all_events: full accumulated history for this chain, oldest-first.
        stats: dict with new_this_run, total_events, prior_watermark, new_watermark.
    """
    own_store = store is None
    if store is None:
        store = EventStore(db_path) if db_path else EventStore()

    try:
        prior_watermark = store.get_watermark(chain, source)

        new_count = store.add_events(events)

        # Advance the watermark to the latest event timestamp we've now stored.
        latest_ts = None
        for e in events:
            ts_iso = e.ts.isoformat() if hasattr(e.ts, "isoformat") else str(e.ts)
            if latest_ts is None or ts_iso > latest_ts:
                latest_ts = ts_iso
        if latest_ts:
            store.set_watermark(chain, source, latest_ts)

        all_events = store.all_events(chain)
        stats = {
            "new_this_run": new_count,
            "total_events": len(all_events),
            "prior_watermark": prior_watermark,
            "new_watermark": store.get_watermark(chain, source),
        }
        return all_events, stats
    finally:
        if own_store:
            store.close()


def status(db_path: Optional[str] = None) -> List[dict]:
    """Return per-source ingest progress for a 'how far along am I' readout."""
    store = EventStore(db_path) if db_path else EventStore()
    try:
        return store.state_summary()
    finally:
        store.close()
