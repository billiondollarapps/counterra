"""Shared HTML report renderer for Counterra."""


def bar_rows(d, total, color="#2f6fed", fmt="${:,.2f}"):
    rows = ""
    for k, v in sorted(d.items(), key=lambda kv: -kv[1]):
        pct = (v / total * 100) if total else 0
        rows += f"""<tr><td class="k">{k}</td>
        <td class="bar"><div style="width:{pct:.1f}%;background:{color}"></div></td>
        <td class="v">{fmt.format(v)}</td><td class="p">{pct:.0f}%</td></tr>"""
    return rows


def render(summary, entries, exc, period, entity_label="Demo Co (simulated data)"):
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
    xrows = "".join(
        f"""<tr><td>{x['ts'][:16].replace('T',' ')}</td><td>{x['agent']}</td>
        <td>{x['provider']}</td><td class="num">${x['amount_usdc']:,.2f}</td>
        <td>{x['reason']}</td></tr>""" for x in exc[:12]
    )
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
    <div class="s">Period {period} &nbsp;&middot;&nbsp; Chain: Base &nbsp;&middot;&nbsp; Protocol: x402 &nbsp;&middot;&nbsp; Entity: {entity_label}</div></div>
    <div class="wrap">
      <div class="cards">
        <div class="card"><div class="l">Total agent spend</div><div class="v">${summary['total']:,.2f}</div></div>
        <div class="card"><div class="l">Settlements ingested</div><div class="v">{summary['n_events']:,}</div></div>
        <div class="card"><div class="l">Journal entries produced</div><div class="v">{len(entries)}</div></div>
        <div class="card"><div class="l">Exceptions flagged</div><div class="v">{len(exc)}</div></div>
      </div>
      <h2>Daily spend</h2><div class="spark">{spark}</div>
      <h2>Spend by agent</h2><table>{bar_rows(summary['by_agent'], summary['total'])}</table>
      <h2>Spend by provider</h2><table>{bar_rows(summary['by_provider'], summary['total'], '#177245')}</table>
      <h2>Aggregated journal entries — {period} (Dr expense / Cr Digital Assets USDC)</h2>
      <table><tr><th>Debit</th><th>Credit</th><th style="text-align:right">Amount</th><th>Provider</th><th style="text-align:right">Settlements</th></tr>{jrows}</table>
      <div class="note">Each settlement is a potential digital-asset disposal for tax purposes; disposal counts retained per entry. Sub-materiality amounts swept to period-end rollup.</div>
      <h2>Exception queue (first 12)</h2>
      <table><tr><th>Time</th><th>Agent</th><th>Counterparty</th><th style="text-align:right">Amount</th><th>Reason</th></tr>{xrows}</table>
    </div>
    <div class="foot"><span><b>COUNTERRA v0.3</b> — agents move the money, Counterra makes it count</span>
    <span>Generated from {summary['n_events']:,} canonical PaymentEvents</span></div>
    </body></html>"""
    return html


