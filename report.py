"""Shared HTML report renderer for Counterra."""


def fmt_usd(v):
    """
    Format a USD amount so sub-cent agent payments stay readable.

    Agent spend is dominated by micropayments — the median x402 settlement is
    a fraction of a cent — so a fixed 2-decimal format renders most of a real
    report as "$0.00", which reads as broken rather than small. Scale the
    precision to the magnitude instead.
    """
    try:
        a = abs(float(v))
    except (TypeError, ValueError):
        return "$0.00"
    if a == 0:
        return "$0.00"
    if a < 0.01:
        return f"${v:,.6f}".rstrip("0").rstrip(".")
    if a < 1:
        return f"${v:,.4f}".rstrip("0").rstrip(".")
    return f"${v:,.2f}"


def bar_rows(d, total, color="#2f6fed", fmt=None, top=12):
    """
    Render a ranked bar table, collapsing the long tail into one summary row.

    Agent-spend distributions have a very long tail of sub-cent counterparties;
    listing all of them (77 rows of "$0.00 0%") buries the signal. Show the top
    `top` entries and roll the remainder into a single line that still accounts
    for every dollar, so nothing is silently dropped.
    """
    items = sorted(d.items(), key=lambda kv: -kv[1])
    head, tail = items[:top], items[top:]
    rows = ""
    for k, v in head:
        pct = (v / total * 100) if total else 0
        label = fmt.format(v) if fmt else fmt_usd(v)
        rows += f"""<tr><td class="k">{k}</td>
        <td class="bar"><div style="width:{pct:.1f}%;background:{color}"></div></td>
        <td class="v">{label}</td><td class="p">{pct:.1f}%</td></tr>"""
    if tail:
        tsum = sum(v for _, v in tail)
        pct = (tsum / total * 100) if total else 0
        label = fmt.format(tsum) if fmt else fmt_usd(tsum)
        rows += f"""<tr><td class="k" style="font-style:italic;color:#5f6672">
        and {len(tail):,} more (long tail)</td>
        <td class="bar"><div style="width:{pct:.1f}%;background:#b9c0cc"></div></td>
        <td class="v">{label}</td><td class="p">{pct:.1f}%</td></tr>"""
    return rows


def render(summary, entries, exc, period, entity_label="Demo Co (simulated data)",
           chain_name="Base", attribution=None, grouped_exc=None):
    daily = summary["by_day"]
    maxd = max(daily.values()) if daily else 1
    spark = "".join(
        f'<div class="d" style="height:{max(3, v/maxd*46):.0f}px" title="{k}: ${v:,.2f}"></div>'
        for k, v in daily.items()
    )
    jrows = "".join(
        f"""<tr><td>{e['debit_account']}</td><td>{e['credit_account']}</td>
        <td class="num">${e['amount_usd']:,.2f}</td><td>{e['provider']}</td>
        <td class="num">{e['settlements']:,}</td></tr>""" for e in entries
    )
    gx = grouped_exc if grouped_exc is not None else []
    xrows = "".join(
        f"""<tr><td>{x['counterparty'][:20]}…</td>
        <td class="num">{x['settlements']:,}</td>
        <td class="num">{x['distinct_payers']:,}</td>
        <td class="num">{fmt_usd(x['amount_usdc'])}</td>
        <td>{x['first_ts'][:10]} → {x['last_ts'][:10]}</td>
        <td>{x['reason']}</td></tr>""" for x in gx[:15]
    )
    # ERC-8021 attribution block (omitted entirely when nothing was captured)
    attr_html = ""
    if attribution and attribution.get("total_rows"):
        cov = attribution["coverage"]
        def _tbl(d, label):
            if not d:
                return ""
            rows_ = "".join(
                f"""<tr><td>{code}</td><td class="num">${v['amount']:,.6f}</td>
                <td class="num">{v['settlements']:,}</td></tr>"""
                for code, v in sorted(d.items(), key=lambda kv: -kv[1]["amount"])[:10]
            )
            return (f"<h3 style='font-size:12px;color:var(--mut);margin:10px 0 4px'>{label}</h3>"
                    f"<table><tr><th>Code</th><th class='num'>Amount</th>"
                    f"<th class='num'>Settlements</th></tr>{rows_}</table>")
        blocks = (_tbl(attribution["apps"], "Seller app codes (a)")
                  + _tbl(attribution["facilitators"], "Facilitator codes (w)")
                  + _tbl(attribution["services"], "Client/service codes (s)"))
        if blocks:
            attr_html = f"""
      <h2>ERC-8021 attribution (decoded from settlement calldata)</h2>
      <p style="font-size:11px;color:var(--mut);margin-bottom:6px">
        Coverage: app {cov['app']*100:.0f}% &middot; facilitator {cov['facilitator']*100:.0f}%
        &middot; client {cov['service']*100:.0f}% of {attribution['total_rows']:,} settlements.
        Attribution is opt-in per payment, so partial coverage is expected.
      </p>{blocks}"""

    html = f"""<!DOCTYPE html><html><head><meta charset="utf-8"><style>
    :root{{--navy:#15243b;--acc:#2f6fed;--mut:#5f6672;--line:#e3e6ec;--bg:#f7f8fa}}
    *{{margin:0;padding:0;box-sizing:border-box}}
    body{{font-family:'Segoe UI',Arial,sans-serif;color:#1c1e22;background:#fff;padding:0}}
    .hd{{background:var(--navy);color:#fff;padding:26px 36px}}
    .hd .k{{font-size:10px;letter-spacing:3px;color:#8fb3f5;text-transform:uppercase}}
    .hd h1{{font-size:24px;margin-top:4px}} .hd .s{{color:#c8d4e8;font-size:12px;margin-top:5px}}
    .wrap{{padding:24px 36px}}
    .cards{{display:flex;gap:14px;margin-bottom:22px}}
    .card{{flex:1;border:1px solid var(--line);border-top:3px solid var(--acc);border-radius:6px;padding:12px 16px;background:var(--bg)}}
    .card .l{{font-size:10px;letter-spacing:1.5px;text-transform:uppercase;color:var(--mut)}}
    .card .v{{font-size:22px;font-weight:700;color:var(--navy);margin-top:3px}}
    h2{{font-size:11px;letter-spacing:2.5px;text-transform:uppercase;color:#1f4fb0;margin:20px 0 8px}}
    table{{width:100%;border-collapse:collapse;font-size:12px}}
    td,th{{padding:6px 8px;border-bottom:1px solid var(--line);text-align:left;vertical-align:middle}}
    th{{font-size:9.5px;letter-spacing:1.5px;text-transform:uppercase;color:var(--mut)}}
    td.num,td.v{{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}}
    td.k{{width:160px}} td.p{{width:40px;text-align:right;color:var(--mut)}}
    td.bar{{width:45%}} td.bar div{{height:12px;border-radius:3px;min-width:2px}}
    .spark{{display:flex;align-items:flex-end;gap:2px;height:50px;margin:6px 0 2px}}
    .spark .d{{flex:1;background:#9db9f0;border-radius:2px 2px 0 0}}
    .foot{{margin-top:26px;padding:12px 36px;background:var(--bg);border-top:2px solid var(--acc);
      font-size:10px;color:var(--mut);display:flex;justify-content:space-between}}
    .note{{font-size:10.5px;color:var(--mut);margin-top:4px;font-style:italic}}
    </style></head><body>
    <div class="hd"><div class="k">Counterra &middot; Agent Spend Report</div>
    <h1>Agentic Commerce — Monthly Close</h1>
    <div class="s">Period {period} &nbsp;&middot;&nbsp; Chain: {chain_name} &nbsp;&middot;&nbsp; Protocol: x402 &nbsp;&middot;&nbsp; Entity: {entity_label}</div></div>
    <div class="wrap">
      <div class="cards">
        <div class="card"><div class="l">Total agent spend</div><div class="v">${summary['total']:,.2f}</div></div>
        <div class="card"><div class="l">Settlements ingested</div><div class="v">{summary['n_events']:,}</div></div>
        <div class="card"><div class="l">Journal entries produced</div><div class="v">{len(entries)}</div></div>
        <div class="card"><div class="l">Exceptions flagged</div><div class="v">{len(gx)}</div></div>
      </div>
      <h2>Daily spend</h2><div class="spark">{spark}</div>
      <h2>Spend by agent</h2><table>{bar_rows(summary['by_agent'], summary['total'])}</table>
      <h2>Spend by provider</h2><table>{bar_rows(summary['by_provider'], summary['total'], '#177245')}</table>
      <h2>Aggregated journal entries — {period} (Dr expense / Cr Digital Assets USDC)</h2>
      <table><tr><th>Debit</th><th>Credit</th><th style="text-align:right">Amount</th><th>Provider</th><th style="text-align:right">Settlements</th></tr>{jrows}</table>
      <div class="note">Each settlement is a potential digital-asset disposal for tax purposes; disposal counts retained per entry. Sub-materiality amounts swept to period-end rollup.</div>{attr_html}
      <h2>Exception queue — {len(gx)} item(s), grouped by counterparty</h2>
      <p style="font-size:11px;color:var(--mut);margin-bottom:6px">
        One row per underlying problem, not per settlement: repeated payments to the
        same unmapped counterparty are a single classification task.</p>
      <table><tr><th>Counterparty</th><th style="text-align:right">Settlements</th>
      <th style="text-align:right">Payers</th><th style="text-align:right">Amount</th>
      <th>Window</th><th>Reason</th></tr>{xrows}</table>
    </div>
    <div class="foot"><span><b>COUNTERRA v0.3</b> — agents move the money, Counterra makes it count</span>
    <span>Generated from {summary['n_events']:,} canonical PaymentEvents</span></div>
    </body></html>"""
    return html


