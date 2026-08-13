#!/usr/bin/env python3
# Counterra network sweep — builds a real trailing x402 settlement rate on Base.
#
# Runs on a schedule (GitHub Actions cron). Each run samples the most recent
# facilitator settlements, DEDUPES them by (tx_hash, log_index) against a rolling
# buffer, trims the buffer to a retention window, and recomputes 24h / 7d rates.
# Because consecutive runs overlap, summing per-run volume would double-count —
# dedup by settlement identity is what makes the cumulative and rate honest.
#
# Python 3.8-safe: stdlib only (urllib), no list[]/dict[] typing syntax.

import json, os, re, time, urllib.request, urllib.error
from datetime import datetime, timezone

USDC = "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913"
BS = "https://base.blockscout.com/api/v2"
REGISTRY = ("https://raw.githubusercontent.com/Merit-Systems/x402scan/"
            "main/packages/external/facilitators/src/facilitators/coinbase.ts")
FALLBACK_FACS = [
    "0x2a89407a98a0732b7fd578c4e156b7166540eb5a",
    "0xe72f0af4cf41356d433723547f1412ca27fbb1b8",
    "0xca5e87f82b3fa093800e6ad67d621a427d79c70d",
    "0x4c934c63c786157fefd990945b25ea60a0fb0205",
]
TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"

OUT = os.environ.get("STATS_PATH", "docs/network-stats.json")
PROVIDERS_PATH = os.environ.get("PROVIDERS_PATH", "docs/providers.json")
TX_PER_RUN = int(os.environ.get("TX_PER_RUN", "80"))   # log fetches per run (rate-limit budget)
RETAIN_DAYS = float(os.environ.get("RETAIN_DAYS", "14"))
BUFFER_CAP = int(os.environ.get("BUFFER_CAP", "6000"))

UA = {"User-Agent": "counterra-sweep/1.0"}


def get_json(url, tries=4):
    last = None
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            last = e
            if e.code == 429:
                time.sleep(1.5 * (i + 1))  # back off on rate limit
                continue
            if 500 <= e.code < 600:
                time.sleep(1.0 * (i + 1))
                continue
            raise
        except Exception as e:
            last = e
            time.sleep(0.8 * (i + 1))
    if last:
        raise last


def get_text(url):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read().decode("utf-8")


def facilitators():
    try:
        t = get_text(REGISTRY)
        pairs = re.findall(r"address:\s*'(0x[0-9a-fA-F]{40})'[\s\S]*?Date\('([\d-]+)'\)", t)
        pairs = sorted(pairs, key=lambda p: p[1], reverse=True)
        facs = [p[0].lower() for p in pairs][:6]
        if facs:
            return facs
    except Exception:
        pass
    return [f.lower() for f in FALLBACK_FACS]


def load_providers():
    m = {}
    try:
        with open(PROVIDERS_PATH, "r") as f:
            reg = json.load(f)
        for p in reg.get("providers", []):
            w = p["wallet"]
            key = w.lower() if w.startswith("0x") else w
            m[key] = {"label": p.get("label"), "category": p.get("category")}
    except Exception:
        pass
    return m


def topic_addr(t):
    return ("0x" + t[-40:]).lower()


def sweep(facs, fac_set):
    """Return list of legs: {id, ts, payer, seller, amount}."""
    legs = []
    seen_tx = set()
    scanned = 0
    for fac in facs:
        try:
            d = get_json(BS + "/addresses/" + fac + "/transactions?filter=from")
        except Exception:
            continue
        for it in (d.get("items") or []):
            to = ((it.get("to") or {}).get("hash") or "").lower()
            if to != USDC or (it.get("status", "ok") != "ok"):
                continue
            h = it.get("hash")
            if not h or h in seen_tx:
                continue
            seen_tx.add(h)
            if scanned >= TX_PER_RUN:
                break
            ts = it.get("timestamp")
            try:
                logs = get_json(BS + "/transactions/" + h + "/logs")
            except Exception:
                continue
            scanned += 1
            for l in (logs.get("items") or []):
                addr = l.get("address")
                a = (addr.get("hash") if isinstance(addr, dict) else addr) or ""
                if a.lower() != USDC:
                    continue
                tp = l.get("topics") or []
                if len(tp) < 3 or not tp[0] or tp[0].lower() != TRANSFER_TOPIC or not tp[1] or not tp[2]:
                    continue
                payer = topic_addr(tp[1])
                seller = topic_addr(tp[2])
                if payer in fac_set or seller in fac_set:
                    continue
                try:
                    amt = int(l.get("data"), 16) / 1e6
                except Exception:
                    amt = 0.0
                if amt <= 0:
                    continue
                idx = l.get("index", l.get("log_index", 0))
                legs.append({
                    "id": h + "-" + str(idx),
                    "ts": ts,
                    "payer": payer,
                    "seller": seller,
                    "amount": amt,
                })
            time.sleep(0.2)  # be gentle on the public endpoint
        if scanned >= TX_PER_RUN:
            break
    return legs, scanned


def parse_ts(s):
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except Exception:
        return None


def derive(buffer, providers, now_ts):
    def window(seconds):
        cutoff = now_ts - seconds
        vol = 0.0
        n = 0
        named_vol = 0.0
        agents = set()
        sellers = set()
        for e in buffer:
            t = parse_ts(e.get("ts"))
            if t is None or t < cutoff:
                continue
            vol += e["amount"]
            n += 1
            agents.add(e["payer"])
            sellers.add(e["seller"])
            if providers.get(e["seller"]):
                named_vol += e["amount"]
        return vol, n, named_vol, len(agents), len(sellers)

    v24, n24, _, a24, s24 = window(86400)
    v7, n7, nv7, a7, s7 = window(7 * 86400)

    # rate uses the actual observed span of the buffer, capped to 7d
    ts_all = [parse_ts(e.get("ts")) for e in buffer]
    ts_all = [t for t in ts_all if t is not None]
    if len(ts_all) >= 2:
        span_days = max((max(ts_all) - min(ts_all)) / 86400.0, 1e-9)
        span_days = min(span_days, 7.0)
        cutoff = now_ts - span_days * 86400
        span_vol = sum(e["amount"] for e in buffer if (parse_ts(e.get("ts")) or 0) >= cutoff)
        span_n = sum(1 for e in buffer if (parse_ts(e.get("ts")) or 0) >= cutoff)
        rate_vol = span_vol / span_days
        rate_n = span_n / span_days
    else:
        span_days = 0.0
        rate_vol = rate_n = 0.0

    return {
        "volume_24h_usdc": round(v24, 4),
        "settlements_24h": n24,
        "unique_agents_24h": a24,
        "unique_sellers_24h": s24,
        "volume_7d_usdc": round(v7, 4),
        "settlements_7d": n7,
        "unique_agents_7d": a7,
        "unique_sellers_7d": s7,
        "named_pct_7d": round((nv7 / v7 * 100) if v7 else 0.0, 1),
        "observed_span_days": round(span_days, 3),
        "rate_per_day_usdc": round(rate_vol, 4),
        "rate_per_day_settlements": round(rate_n, 2),
    }


def load_stats():
    try:
        with open(OUT, "r") as f:
            return json.load(f)
    except Exception:
        return {"buffer": [], "history": []}


def main():
    now = datetime.now(timezone.utc)
    now_ts = now.timestamp()
    facs = facilitators()
    fac_set = set(facs)
    providers = load_providers()

    legs, scanned = sweep(facs, fac_set)

    stats = load_stats()
    buffer = stats.get("buffer", [])
    have = set(e["id"] for e in buffer)
    added = 0
    for lg in legs:
        if lg["id"] not in have:
            buffer.append(lg)
            have.add(lg["id"])
            added += 1

    # trim to retention window + hard cap
    cutoff = now_ts - RETAIN_DAYS * 86400
    buffer = [e for e in buffer if (parse_ts(e.get("ts")) or now_ts) >= cutoff]
    buffer.sort(key=lambda e: parse_ts(e.get("ts")) or 0)
    if len(buffer) > BUFFER_CAP:
        buffer = buffer[-BUFFER_CAP:]

    d = derive(buffer, providers, now_ts)

    # daily history (one row per UTC date, updated in place), capped at 90 days
    history = stats.get("history", [])
    today = now.strftime("%Y-%m-%d")
    row = {"date": today, "volume_24h_usdc": d["volume_24h_usdc"],
           "settlements_24h": d["settlements_24h"],
           "rate_per_day_usdc": d["rate_per_day_usdc"]}
    if history and history[-1].get("date") == today:
        history[-1] = row
    else:
        history.append(row)
    history = history[-90:]

    out = {
        "updated_at": now.isoformat(),
        "chain": "base",
        "source": "coinbase facilitators via blockscout",
        "facilitators": len(facs),
        "note": ("Coinbase facilitators on Base only; excludes other facilitators and Solana. "
                 "A floor, not a census. Rates are trailing, dedup by settlement id."),
        "run": {"scanned_tx": scanned, "legs_seen": len(legs), "new_settlements": added,
                "buffer_size": len(buffer)},
        "derived": d,
        "history": history,
        "buffer": buffer,
    }

    os.makedirs(os.path.dirname(OUT) or ".", exist_ok=True)
    with open(OUT, "w") as f:
        json.dump(out, f, indent=2)

    print("swept {} tx, {} legs, +{} new, buffer {} | 7d ${} / {} settlements | ~${}/day".format(
        scanned, len(legs), added, len(buffer),
        d["volume_7d_usdc"], d["settlements_7d"], d["rate_per_day_usdc"]))


if __name__ == "__main__":
    main()
