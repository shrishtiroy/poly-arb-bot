"""polyarb dashboard: the product view over the paper-trading DB.

Serves two pages from the local SQLite DB with the stdlib http.server (no extra
dependencies):

  /        Live gaps: every detected gap, showing whether the disciplined taker
           (cross the spread only when net edge after fees is positive) took it,
           the price of each leg, the fee math, and a link back to the live
           market so you can act on it.
  /about   How it works: a teaching page on arbitrage, the gap, fees, taker vs
           maker, leg risk, and cross-venue gaps.

Only the disciplined taker is surfaced. The naive-taker and maker policies stay
in the engine (they are how the harness proves the naive strategy loses money)
but are not shown here.

Run:  polyarb dashboard --db ~/polyarb-data/live.sqlite
"""
from __future__ import annotations

import datetime
import json
import sqlite3
from http.server import BaseHTTPRequestHandler, HTTPServer

POLICY = "taker_disc"  # the disciplined taker


def market_url(venue: str, slug: str | None, event_slug: str | None) -> str | None:
    """Build a link to the live market page, or None if we lack a slug."""
    if venue == "polymarket":
        if event_slug:
            return f"https://polymarket.com/event/{event_slug}"
        if slug:
            return f"https://polymarket.com/market/{slug}"
        return None
    if venue == "kalshi" and slug:
        return f"https://kalshi.com/markets/{slug}"
    return None


def load(db):
    try:
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    except sqlite3.OperationalError:
        return None
    conn.row_factory = sqlite3.Row

    # market_id -> {question, url}. outcome_id -> outcome name.
    qmap, omap, urlmap = {}, {}, {}
    try:
        for m in conn.execute("SELECT * FROM markets"):
            qmap[m["market_id"]] = m["question"]
            keys = m.keys()
            slug = m["slug"] if "slug" in keys else None
            event_slug = m["event_slug"] if "event_slug" in keys else None
            urlmap[m["market_id"]] = market_url(m["venue"], slug, event_slug)
            for oid, name in json.loads(m["outcomes"]).items():
                omap[oid] = name
    except sqlite3.OperationalError:
        pass

    # Disciplined-taker P&L, keyed by gap. Prefer the real taker_disc row; for
    # gaps recorded before that policy existed, fall back to the naive taker
    # row (identical numbers: discipline only decides WHETHER to take it).
    disc, naive = {}, {}
    for r in conn.execute("SELECT * FROM results WHERE policy IN (?,?)",
                          (POLICY, "taker")):
        (disc if r["policy"] == POLICY else naive)[r["gap_id"]] = r

    # Maker resting limit prices per gap, keyed outcome_id -> price, so the
    # expanded view can show what the parallel maker attempt rested at. The
    # `detail` column only exists on DBs created after this feature landed.
    maker_by_gap: dict[str, dict] = {}
    try:
        rows = conn.execute(
            "SELECT gap_id, detail FROM results WHERE policy='maker'").fetchall()
    except sqlite3.OperationalError:
        rows = []
    for r in rows:
        raw = r["detail"]
        if raw:
            try:
                maker_by_gap[r["gap_id"]] = json.loads(raw)
            except (TypeError, ValueError):
                pass

    today = datetime.date.today()

    def make_gap(g, idx):
        """Build one display row from a gap_events row (+ its taker result)."""
        legs = json.loads(g["legs"])
        legdet = [{
            "name": omap.get(l["outcome_id"], l["outcome_id"][:10] + "..."),
            "price": l["avg_price"],
            "question": qmap.get(l["market_id"], "?"),
            "url": urlmap.get(l["market_id"]),
            # levels: [[price, size], ...] walked to fill; empty on older rows.
            "levels": l.get("levels") or [],
            "outcome_id": l["outcome_id"],
        } for l in legs]
        traded = g["net_taker_edge_per_share"] > 0
        r = disc.get(g["gap_id"]) or (naive.get(g["gap_id"]) if traded else None)
        traded = traded and r is not None
        return dict(
            gap_id=g["gap_id"], ts=g["ts"], kind=g["kind"],
            question=qmap.get(legs[0]["market_id"], "?") if legs else "?",
            legs=legdet, ask_sum=sum(l["avg_price"] for l in legs),
            gross_c=g["gross_edge_per_share"] * 100,
            fee_c=g["taker_fees_per_share"] * 100,
            net_c=g["net_taker_edge_per_share"] * 100,
            traded=traded,
            pnl=r["net_pnl"] if traded else None,
            fees=r["fees"] if traded else 0.0,
            shares=r["shares"] if traded else g["executable_shares"],
            maker_limits=maker_by_gap.get(g["gap_id"]),
            top20=idx < 20,
            is_today=datetime.datetime.fromtimestamp(g["ts"]).date() == today,
        )

    # Split gaps by kind: ladder gaps live on their own page; binary/multi are
    # the "taker" strategy shown on the main page.
    gaps, ladder_gaps = [], []
    for g in conn.execute("SELECT * FROM gap_events ORDER BY ts DESC LIMIT 1000"):
        bucket = ladder_gaps if g["kind"] == "ladder" else gaps
        bucket.append(make_gap(g, len(bucket)))

    def summarize(rows):
        taken = [x for x in rows if x["traded"]]
        return dict(
            rows=rows, took=len(taken), skipped=len(rows) - len(taken),
            total=len(rows), net=sum(x["pnl"] for x in taken),
            fees=sum(x["fees"] for x in taken),
            takers=sorted(
                [dict(ts=x["ts"], pnl=x["pnl"], row_idx=i, question=x["question"])
                 for i, x in enumerate(rows) if x["traded"]],
                key=lambda t: t["ts"]),
        )

    taker = summarize(gaps)
    ladder = summarize(ladder_gaps)

    # The headline totals must not be bounded by the LIMIT 1000 above (that
    # limit exists only to cap the page size of the row table/chart). Without
    # this, "Total P&L" silently became a rolling-most-recent-1000 number
    # instead of a lifetime total, which is why it swung from ~14,000 to
    # ~3,000 the moment three weeks of paused publishing caught up at once.
    for kind, bucket in (("taker", taker), ("ladder", ladder)):
        cond = "g.kind = 'ladder'" if kind == "ladder" else "g.kind != 'ladder'"
        row = conn.execute(f"""
            SELECT
              COUNT(*) AS total,
              SUM(traded) AS took,
              SUM(CASE WHEN traded THEN pnl ELSE 0 END) AS net,
              SUM(CASE WHEN traded THEN fees ELSE 0 END) AS fees
            FROM (
              SELECT
                (g.net_taker_edge_per_share > 0
                 AND COALESCE(rd.gap_id, rn.gap_id) IS NOT NULL) AS traded,
                COALESCE(rd.net_pnl, rn.net_pnl) AS pnl,
                COALESCE(rd.fees, rn.fees) AS fees
              FROM gap_events g
              LEFT JOIN results rd ON rd.gap_id = g.gap_id AND rd.policy = ?
              LEFT JOIN results rn ON rn.gap_id = g.gap_id AND rn.policy = 'taker'
              WHERE {cond}
            )""", (POLICY,)).fetchone()
        total, took = row["total"] or 0, row["took"] or 0
        bucket.update(total=total, took=took, skipped=total - took,
                      net=row["net"] or 0.0, fees=row["fees"] or 0.0)

    # Maker attempts, oldest first, for the cumulative maker chart. row_idx links
    # each point back to its gap's row in the main table (None if outside it).
    main_row = {x["gap_id"]: i for i, x in enumerate(gaps)}
    maker = []
    for r in conn.execute(
            "SELECT * FROM results WHERE policy='maker' ORDER BY ts_close ASC"):
        maker.append(dict(
            gap_id=r["gap_id"], outcome=r["outcome"], net_pnl=r["net_pnl"],
            fees=r["fees"], shares=r["shares"], ts_close=r["ts_close"],
            row_idx=main_row.get(r["gap_id"]),
        ))
    maker_net = sum(a["net_pnl"] for a in maker)
    conn.close()
    return dict(
        gaps=gaps, took=taker["took"], skipped=taker["skipped"],
        total=taker["total"], net=taker["net"], fees=taker["fees"],
        takers=taker["takers"], maker=maker, maker_net=maker_net,
        ladder=ladder, omap=omap)


def m(x):
    return f'<span class="{"pos" if x >= 0 else "neg"}">{x:+,.2f}</span>'


def cumulative_chart(points, dot_class, label, dots=True, trend=False, xlabels=None):
    """Inline SVG: a cumulative-P&L line plus one dot per point, oldest to
    newest. Each point is dict(net, row_idx, dcls?, title). dot_class is the
    default CSS class for a dot; a point may override with its own 'dcls'. Dots
    link to their table row. Pass dots=False for a plain line graph (used when
    the points are not individually clickable). Hand-built so the dashboard
    stays stdlib (no JS charting library)."""
    if not points:
        return ""
    cum, running = [], 0.0
    for p in points:
        running += p["net"]
        cum.append(running)

    W, H = 900, 300
    padL, padR, padT, padB = 58, 22, 22, 34
    iw, ih = W - padL - padR, H - padT - padB
    n = len(points)
    lo = min(0.0, min(cum))
    hi = max(0.0, max(cum))
    if hi == lo:
        hi = lo + 1.0
    span = hi - lo

    def px(i):
        return padL + (iw * i / (n - 1) if n > 1 else iw / 2)

    def py(v):
        return padT + ih * (hi - v) / span

    # Horizontal gridlines + $ labels (including a bold zero baseline).
    grid = ""
    steps = 4
    for s in range(steps + 1):
        v = hi - span * s / steps
        y = py(v)
        zero = abs(v) < 1e-9
        grid += (f'<line x1="{padL}" y1="{y:.1f}" x2="{W - padR}" y2="{y:.1f}" '
                 f'class="{"axzero" if zero else "axgrid"}"/>'
                 f'<text x="{padL - 8}" y="{y + 4:.1f}" class="axlbl">${v:,.0f}</text>')

    line = " ".join(f"{px(i):.1f},{py(cum[i]):.1f}" for i in range(n))
    poly = f'<polyline points="{line}" class="pnlline"/>'

    # Least-squares trendline over the cumulative curve (dashed overlay).
    trend_svg = ""
    if trend and n >= 2:
        xs = list(range(n))
        mx = sum(xs) / n
        my = sum(cum) / n
        denom = sum((x - mx) ** 2 for x in xs)
        if denom > 0:
            slope = sum((xs[i] - mx) * (cum[i] - my) for i in range(n)) / denom
            b = my - slope * mx
            y0, y1 = b, slope * (n - 1) + b
            trend_svg = (f'<line x1="{px(0):.1f}" y1="{py(y0):.1f}" '
                         f'x2="{px(n-1):.1f}" y2="{py(y1):.1f}" class="trendline"/>')

    dot_svg = ""
    if dots:
        for i, p in enumerate(points):
            cls = p.get("dcls", dot_class)
            cx, cy = px(i), py(cum[i])
            title = p.get("title", f'net {p["net"]:+.2f} · cumulative {cum[i]:+.2f}')
            circle = f'<circle cx="{cx:.1f}" cy="{cy:.1f}" r="4.5" class="{cls}"><title>{title}</title></circle>'
            if p.get("row_idx") is not None:
                dot_svg += (f'<a href="#row{p["row_idx"]}" '
                            f'onclick="flash({p["row_idx"]})">{circle}</a>')
            else:
                dot_svg += circle

    return f"""<svg viewBox="0 0 {W} {H}" class="mchart" preserveAspectRatio="xMidYMid meet"
      xmlns="http://www.w3.org/2000/svg" role="img" aria-label="{label}">
      {grid}{trend_svg}{poly}{dot_svg}
      <text x="{padL + 14}" y="{H - 8}" class="axlbl" text-anchor="start">{(xlabels[0] if xlabels else "oldest")}</text>
      <text x="{W - padR}" y="{H - 8}" class="axlbl" text-anchor="end">{(xlabels[1] if xlabels else "newest")}</text>
    </svg>"""


def taker_section(takers, total_net):
    """The disciplined-taker cumulative chart, shown first (above the table)."""
    if not takers:
        return """<h2 class="mh">Taker trades</h2>
          <p class="hint">No profitable taker trades booked yet. When a gap's
          net edge clears its fees, the disciplined taker takes it and it shows
          up here.</p>"""
    points = [dict(net=t["pnl"], row_idx=t["row_idx"],
                   dcls="dpos" if t["pnl"] >= 0 else "dneg",
                   title=f'{t["question"][:60]} · {t["pnl"]:+.2f}')
              for t in takers]
    wins = sum(1 for t in takers if t["pnl"] >= 0)
    avg = total_net / len(takers) if takers else 0.0
    cards = f"""<div class="cards">
      <div class="card"><div class="k">Taker trades</div><div class="v">{len(takers):,}</div></div>
      <div class="card"><div class="k">Cumulative P&amp;L</div><div class="v">{m(total_net)}</div></div>
      <div class="card"><div class="k">Profitable</div><div class="v">{wins}/{len(takers)}</div></div>
      <div class="card"><div class="k">Avg per trade</div><div class="v">{m(avg)}</div></div>
    </div>"""
    return f"""<h2 class="mh first" id="taker-chart">Taker trades</h2>
      <p class="hint">The disciplined taker crosses the spread only when a gap's
      net edge beats its fees. The line is cumulative P&amp;L over time; each
      <span class="lg-pos">green</span> dot is one booked trade.
      <b>Click any dot</b> to jump to that trade in the table below.</p>
      {cumulative_chart(points, "dpos", "Cumulative taker P and L over time")}
      {cards}"""


def maker_chart(maker):
    points = [dict(net=a["net_pnl"], row_idx=a["row_idx"],
                   dcls="dleg" if a["outcome"] == "leg_risk" else "dunf",
                   title=f'{a["outcome"]} · net {a["net_pnl"]:+.2f}')
              for a in maker]
    return cumulative_chart(points, "dunf", "Cumulative maker P and L over time")


def maker_stats(maker):
    """Headline observations computed from the maker attempts."""
    n = len(maker)
    filled = [a for a in maker if a["outcome"] != "unfilled"]
    leg = [a for a in maker if a["outcome"] == "leg_risk"]
    total = sum(a["net_pnl"] for a in maker)
    fill_rate = len(filled) / n * 100 if n else 0.0
    avg_leg = sum(a["net_pnl"] for a in leg) / len(leg) if leg else 0.0
    return dict(n=n, filled=len(filled), leg=len(leg), total=total,
                fill_rate=fill_rate, avg_leg=avg_leg)


def maker_section(maker):
    if not maker:
        return ""
    s = maker_stats(maker)
    cards = f"""<div class="cards">
      <div class="card"><div class="k">Maker attempts</div><div class="v">{s['n']:,}</div></div>
      <div class="card"><div class="k">Fill rate</div><div class="v">{s['fill_rate']:.0f}%</div></div>
      <div class="card"><div class="k">Ended in leg risk</div><div class="v">{s['leg']:,}</div></div>
      <div class="card"><div class="k">Cumulative P&amp;L</div><div class="v">{m(s['total'])}</div></div>
      <div class="card"><div class="k">Avg loss per fill</div><div class="v">{m(s['avg_leg'])}</div></div>
    </div>"""
    return f"""<h2 class="mh" id="maker-chart">Maker attempts</h2>
      <p class="hint">The maker rests a limit order on each leg instead of
      crossing the spread. The line is cumulative P&amp;L over time; each dot is
      one attempt. <span class="lg-unf">Grey</span> dots never filled (the
      resting order expired flat); <span class="lg-leg">red</span> dots did fill
      but only on one leg, so they were liquidated at a leg-risk loss.
      <b>Click any dot</b> to jump to that attempt in the table above.</p>
      {maker_chart(maker)}
      {cards}
      <p class="takeaway">The maker almost never gets filled here (fill rate
      about {s['fill_rate']:.0f}%), and the few fills are adverse selection: the
      order trades only when the price is already moving against it, so it lands
      in leg risk and loses. Resting orders are not a free discount on these
      markets, they are a bet on fill timing that this data says you lose.</p>"""


def gap_table(gaps, omap):
    """Render the gap spreadsheet (rows + expandable detail) shared by the main
    page and the ladder page."""
    body_rows = ""
    for i, g in enumerate(gaps):
        ts = datetime.datetime.fromtimestamp(g["ts"]).strftime("%m-%d %H:%M:%S")
        if g["traded"]:
            action = '<span class="took">TRADED</span>'
            pnl = m(g["pnl"])
            rowcls = ""
        else:
            action = '<span class="skip">skipped</span>'
            pnl = '<span class="muted">fees &gt; gap</span>'
            rowcls = "skiprow"
        # Filter tags: shown/hidden client-side by the Top 20 / Today / All view.
        viewcls = ("is-top20 " if g["top20"] else "") + ("is-today" if g["is_today"] else "")
        netcls = "pos" if g["net_c"] > 0 else "neg"
        # Per-leg depth: show the full walked book when we crossed more than one
        # price level, else just the single ask/avg (also the fallback for old
        # rows that stored no levels).
        legrows = ""
        for l in g["legs"]:
            lv = l["levels"]
            if lv and len(lv) > 1:
                tiers = " ".join(
                    f"<span class='tier'>{sz:,.0f} @ {pr:.3f}</span>" for pr, sz in lv)
                legrows += (f"<div class='leg deep'><div class='legtop'>"
                            f"<span class='on'>{l['name']}</span>"
                            f"<span class='pr'>avg {l['price']:.3f}</span></div>"
                            f"<div class='tiers'>{tiers}</div></div>")
            else:
                legrows += (f"<div class='leg'><span class='on'>{l['name']}</span>"
                            f"<span class='pr'>{l['price']:.3f}</span></div>")
        # One link per distinct market in the legs (binary = 1, multi/ladder = many).
        seen, linkitems = set(), []
        for l in g["legs"]:
            q = l["question"]
            if q in seen:
                continue
            seen.add(q)
            if l["url"]:
                linkitems.append(
                    f"<a class='mlink' href='{l['url']}' target='_blank' "
                    f"rel='noopener' onclick='event.stopPropagation()'>"
                    f"{q} <span class='arrow'>open market -&gt;</span></a>")
            else:
                linkitems.append(f"<span class='mlink dead'>{q}</span>")
        links = "".join(linkitems)
        exec_label = ("Taker trade (crossed the spread)" if g["traded"]
                      else "Not taken (net edge did not clear fees)")
        maker_line = ""
        if g["maker_limits"]:
            parts = ", ".join(
                f"{omap.get(oid, oid[:8] + '...')} @ {pr:.3f}"
                for oid, pr in g["maker_limits"].items())
            maker_line = (f"<div class='makerline'>Maker also rested a limit "
                          f"order at: {parts}</div>")
        # Ladder gaps get a probability-comparison block plus put-call math that
        # names the YES and NO legs. Other kinds keep the plain ask-sum math.
        if g["kind"] == "ladder" and len(g["legs"]) == 2:
            easier, harder = g["legs"][0], g["legs"][1]
            pe = easier["price"]          # YES ask of the more-probable rung
            n = harder["price"]           # NO ask of the less-probable rung
            ph = 1.0 - n                  # implied YES of the less-probable rung
            math_block = f"""
              <div class="dh">Compare (a YES price is the market's probability)</div>
              <div class="cmp">
                <div class="cmprow cheaper">
                  <span class="cq">{easier['question']}</span>
                  <span class="cp">YES {pe:.3f}</span>
                  <span class="ctag">cheaper, yet more likely</span>
                </div>
                <div class="cmprow">
                  <span class="cq">{harder['question']}</span>
                  <span class="cp">YES {ph:.3f} <em>implied</em></span>
                  <span class="ctag">dearer, yet less likely</span>
                </div>
              </div>
              <div class="cmpnote">The more probable event is trading cheaper. That
                inversion is the mispricing we capture.</div>
              <div class="dh">The math</div>
              <div class="mathline">buy YES <b>{pe:.3f}</b> &nbsp;+&nbsp;
                buy NO <b>{n:.3f}</b> &nbsp;=&nbsp; ask sum <b>{g['ask_sum']:.4f}</b>
                &nbsp;-&nbsp; fees <b>{g['fee_c']:.2f}¢</b>
                &nbsp;=&nbsp; net edge <b class="{netcls}">{g['net_c']:+.2f}¢</b></div>"""
        else:
            math_block = f"""
              <div class="dh">The math</div>
              <div class="mathline">ask sum <b>{g['ask_sum']:.4f}</b> &nbsp;-&nbsp;
                raw gap <b>{g['gross_c']:+.2f}¢</b> &nbsp;less&nbsp; fees <b>{g['fee_c']:.2f}¢</b>
                &nbsp;=&nbsp; net edge <b class="{netcls}">{g['net_c']:+.2f}¢</b></div>"""
        detail = f"""<tr class="detail {viewcls}" id="d{i}"><td colspan="8">
          <button class="collapse" onclick="tog({i})" title="collapse" aria-label="collapse">&#94;</button>
          <div class="dgrid">
            <div class="dcol">
              <div class="dh">Market</div>
              <div class="mlinks">{links}</div>
              <div class="dh">Size and fills ({g['shares']:,.0f} shares per leg)</div>
              <div class="legs">{legrows}</div>
            </div>
            <div class="dcol dmath">
              {math_block}
              <div class="dh">Execution</div>
              <div class="execline">{exec_label}{maker_line}</div>
            </div>
          </div></td></tr>"""
        body_rows += f"""<tr class="row {rowcls} {viewcls}" id="row{i}" onclick="tog({i})">
          <td class="tm">{ts}</td><td class="q">{g['question']}</td>
          <td>{action}</td>
          <td class="r">{g['ask_sum']:.4f}</td><td class="r">{g['gross_c']:+.2f}</td>
          <td class="r">{g['fee_c']:.2f}</td><td class="r {netcls}">{g['net_c']:+.2f}</td>
          <td class="r">{pnl}</td></tr>{detail}"""

    return f"""<div class="viewbar" id="viewbar">
      <button class="pill on" data-v="top20" onclick="setView('top20')">Top 20</button>
      <button class="pill" data-v="today" onclick="setView('today')">Today</button>
      <button class="pill" data-v="all" onclick="setView('all')">All</button>
    </div>
    <table class="sheet"><thead><tr>
      <th>time</th><th>market</th><th>action</th><th class="r">ask sum</th>
      <th class="r">gap¢</th><th class="r">fees¢</th><th class="r">net edge¢</th>
      <th class="r">P&amp;L</th></tr></thead>
      <tbody id="tb" class="view-top20">{body_rows}</tbody></table>"""


def render(db):
    d = load(db)
    if d is None or (d["total"] == 0 and d["ladder"]["total"] == 0):
        return PAGE.format(nav=nav("/"), body='<p class="empty">No gaps detected '
                           'yet. The collector is warming up; refresh shortly.</p>')
    hit = d["took"] / d["total"] * 100 if d["total"] else 0
    # Combined P&L across all three strategies shown on the same DB.
    taker_net, maker_net, ladder_net = d["net"], d["maker_net"], d["ladder"]["net"]
    total_net = taker_net + maker_net + ladder_net
    # Every realized P&L event across all strategies, merged in time order, so
    # the popup can trace total account growth.
    events = ([(t["ts"], t["pnl"]) for t in d["takers"]]
              + [(a["ts_close"], a["net_pnl"]) for a in d["maker"]]
              + [(t["ts"], t["pnl"]) for t in d["ladder"]["takers"]])
    events.sort(key=lambda e: e[0])
    acct_pts = [dict(net=pnl) for _, pnl in events]
    # Even spacing, but label the ends with real dates for temporal context.
    xlabels = None
    if events:
        fmt = lambda ts: datetime.datetime.fromtimestamp(ts).strftime("%b %-d, %-I%p").lower()
        xlabels = (fmt(events[0][0]), fmt(events[-1][0]))
    acct_chart = (cumulative_chart(acct_pts, "dpos", "Total account growth over time",
                                   dots=False, trend=True, xlabels=xlabels)
                  or '<p class="empty">No realized P&amp;L yet.</p>')
    combined = f"""<div class="cards total">
      <div class="card big" onclick="openAcct()" title="View account growth">
        <div class="k">Total paper P&amp;L (all strategies)</div><div class="v">{m(total_net)}</div></div>
      <a class="card cardlink" href="#taker-chart"><div class="k">Taker</div><div class="v">{m(taker_net)}</div></a>
      <a class="card cardlink" href="#maker-chart"><div class="k">Maker</div><div class="v">{m(maker_net)}</div></a>
      <a class="card cardlink" href="/ladders"><div class="k">Ladder arb</div><div class="v">{m(ladder_net)}</div></a>
    </div>
    <div id="acctmodal" class="modal-back" onclick="if(event.target===this)closeAcct()">
      <div class="modal-box">
        <button class="modal-x" onclick="closeAcct()" aria-label="close">&#10005;</button>
        <h2 class="mh first">Total account growth</h2>
        <p class="hint">Every realized trade across the taker, maker and ladder
        strategies, in time order. The line is the running account balance.</p>
        {acct_chart}
      </div>
    </div>"""
    cards = f"""<div class="cards">
      <div class="card"><div class="k">Net P&amp;L (traded only)</div><div class="v">{m(d['net'])}</div></div>
      <div class="card"><div class="k">Trades taken</div><div class="v">{d['took']:,}</div></div>
      <div class="card"><div class="k">Gaps skipped</div><div class="v">{d['skipped']:,}</div></div>
      <div class="card"><div class="k">Take rate</div><div class="v">{hit:.0f}%</div></div>
      <div class="card"><div class="k">Fees paid</div><div class="v">{d['fees']:,.2f}</div></div>
    </div>"""

    body = f"""{combined}
      {taker_section(d['takers'], d['net'])}
      {cards}
      <h2 class="mh">Live gaps</h2>
      <p class="hint">Every detected gap is listed below. A row is
      <b>TRADED</b> only when its net edge beats the fees; greyed
      <b>skipped</b> rows are gaps the strategy correctly walked away from.
      <b>Click a row</b> for the market, its depth walk, and a link to go
      trade it. New to this? Read <a href="/about">How it works</a>.</p>
      {gap_table(d['gaps'], d['omap'])}
      {maker_section(d['maker'])}"""
    return PAGE.format(nav=nav("/"), body=body)


def render_ladders(db):
    d = load(db)
    intro = """<p class="hint">A token's price is the market's probability, so a
      more likely event should cost more. Threshold markets on the same asset
      and date form a ladder where a bigger move is always less likely than a
      smaller one, so the less likely rung should always be the cheaper one.
      When the market misprices this and the more likely rung is trading
      cheaper, we buy that rung and pair it against the other. The payout is
      locked no matter what, so the mispricing is risk-free profit.
      New to this? Read <a href="/about">How it works</a>.</p>"""
    if d is None or d["ladder"]["total"] == 0:
        body = ('<h2 class="mh first">Ladder arbitrage</h2>' + intro +
                '<p class="empty">No ladder inversions detected yet. These are '
                'rare and short-lived; the collector is watching every crypto '
                'threshold ladder and will record one here when it appears.</p>')
        return PAGE.format(nav=nav("/ladders"), body=body)

    lad = d["ladder"]
    section = taker_section(lad["takers"], lad["net"]).replace(
        ">Taker trades<", ">Ladder arbitrage<", 1)
    body = f"""{section}
      {intro}
      <h2 class="mh">Ladder gaps</h2>
      <p class="hint">Each row is a detected rung pair. <b>TRADED</b> rows are
      inversions whose edge beat the fees; expand one to see both rungs and the
      math.</p>
      {gap_table(lad['rows'], d['omap'])}"""
    return PAGE.format(nav=nav("/ladders"), body=body)


def load_fairvalue(db):
    """Read Strategy B measurements. Returns None if the table is absent, else a
    dict of recent rows, per-asset coverage, active hyperparameters, and stats."""
    try:
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    except sqlite3.OperationalError:
        return None
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT * FROM bs_measurements ORDER BY ts DESC LIMIT 500"
        ).fetchall()
        total = conn.execute(
            "SELECT COUNT(*) AS n FROM bs_measurements").fetchone()["n"]
    except sqlite3.OperationalError:
        conn.close()
        return dict(rows=[], total=0, assets=[], params=None, avg_abs_div=0.0)

    recent = [dict(r) for r in rows]
    assets = sorted({r["asset"] for r in recent})
    avg_abs_div = (sum(abs(r["divergence"]) for r in recent) / len(recent)
                   if recent else 0.0)
    # Active hyperparameters: read from the newest row (what the live run used).
    params = recent[0] if recent else None
    conn.close()
    return dict(rows=recent, total=total, assets=assets, params=params,
                avg_abs_div=avg_abs_div)


def render_fairvalue(db):
    data = load_fairvalue(db)
    if data is None:
        return PAGE.format(nav=nav("/fair-value"),
                           body='<p class="empty">No database found.</p>')

    intro = """
    <div class="grid">
      <section class="concept c2 wide">
        <h3><span class="num">B</span>Binary options fair value</h3>
        <p>A Polymarket YES share pays $1 if the outcome happens, so its price is
        the market's implied probability. A crypto threshold market ("Will BTC be
        above $64k on Friday?") is therefore a <b>binary (digital) option</b>. The
        Black-Scholes model gives an independent fair probability from the live
        spot price, the asset's realized volatility, and the time to expiry:
        <code>fair = &#934;(d2)</code>, where
        <code>d2 = (ln(S/K) + (r - &#963;&#178;/2)T) / (&#963;&#8730;T)</code>.</p>
        <p>The <b>divergence</b> is <code>market probability - fair probability</code>.
        A positive divergence means the market prices the event more likely than the
        model does. This is a directional, model-dependent read (the edge is only
        real if the volatility estimate is right), so it is
        <b>measurement-only</b>: no orders, higher variance than arbitrage.</p>
      </section>
    </div>"""

    if not data["rows"]:
        empty = ('<p class="empty">No fair-value measurements recorded yet. They '
                 'appear once the collector has run with a warm spot/volatility '
                 'cache.</p>')
        return PAGE.format(nav=nav("/fair-value"), body=intro + empty)

    p = data["params"]
    stat = (f'<div class="cards">'
            f'<div class="card"><div class="k">Measurements</div>'
            f'<div class="v">{data["total"]:,}</div></div>'
            f'<div class="card"><div class="k">Assets covered</div>'
            f'<div class="v">{len(data["assets"])}</div></div>'
            f'<div class="card"><div class="k">Avg abs divergence</div>'
            f'<div class="v">{data["avg_abs_div"]*100:.1f}&#162;</div></div>'
            f'</div>')

    # Hyperparameters panel: the knobs the recorded numbers were produced with.
    hp = (f'<h2>Hyperparameters</h2>'
          f'<p class="hint">These are recorded with every measurement so runs '
          f'are comparable and the model can be tuned data-driven. Version '
          f'<code>{p["params_version"]}</code>.</p>'
          f'<div class="cards">'
          f'<div class="card"><div class="k">Vol lookback</div>'
          f'<div class="v">{p["vol_lookback_min"]:,} x {p["kline_interval"]}</div></div>'
          f'<div class="card"><div class="k">Annualization</div>'
          f'<div class="v">{p["periods_per_year"]:,.0f}/yr</div></div>'
          f'<div class="card"><div class="k">Risk-free r</div>'
          f'<div class="v">{p["r"]:.2%}</div></div>'
          f'<div class="card"><div class="k">Implied price</div>'
          f'<div class="v">{p["price_mode"]}</div></div>'
          f'</div>')

    def frow(r):
        # Same MM-DD HH:MM:SS format the arb tables use (see gap_table).
        tm = datetime.datetime.fromtimestamp(r["ts"]).strftime("%m-%d %H:%M:%S")
        div = r["divergence"]
        dcls = "pos" if div >= 0 else "neg"
        rel = "above" if r["family"] == "up" else "below"
        return (f'<tr class="row">'
                f'<td class="tm">{tm}</td>'
                f'<td>{r["asset"]}</td>'
                f'<td class="q">{r["question"]}</td>'
                f'<td class="r">${r["spot"]:,.0f}</td>'
                f'<td class="r">{rel} ${r["strike"]:,.0f}</td>'
                f'<td class="r">{r["sigma"]*100:.0f}%</td>'
                f'<td class="r">{r["fair_prob"]*100:.1f}%</td>'
                f'<td class="r">{r["market_prob"]*100:.1f}%</td>'
                f'<td class="r {dcls}">{div*100:+.1f}&#162;</td>'
                f'</tr>')

    table = ('<h2>Recent measurements</h2>'
             '<p class="hint">Divergence is market probability minus model fair '
             'probability. Green means the market prices the event more likely '
             'than the model; red means less likely.</p>'
             '<table class="sheet"><thead><tr>'
             '<th>Time</th><th>Asset</th><th>Market</th>'
             '<th class="r">Spot</th><th class="r">Strike</th>'
             '<th class="r">Vol</th><th class="r">Fair</th>'
             '<th class="r">Market</th><th class="r">Divergence</th>'
             '</tr></thead><tbody>'
             + "".join(frow(r) for r in data["rows"])
             + '</tbody></table>')

    body = intro + stat + hp + table
    return PAGE.format(nav=nav("/fair-value"), body=body)


def render_about():
    body = """
    <div class="grid">

      <section class="concept c1">
        <h3><span class="num">1</span>What is arbitrage</h3>
        <p>A prediction market sells shares in an outcome. A YES share pays
        exactly $1 if it happens and $0 if it does not; NO is the mirror. So the
        price is just the implied probability: YES at 0.63 means "63% likely"
        and pays 1 / 0.63 = 1.59x if it hits.</p>
        <p>The trick: YES + NO together always pay exactly $1 at resolution, no
        matter which side wins.</p>
        <div class="eg"><b>The locked profit.</b> If you can buy the complete
        set for less than $1, the difference is yours, risk-free. You never need
        a buyer: you just hold to resolution and redeem for $1.</div>
      </section>

      <section class="concept c2">
        <h3><span class="num">2</span>The gap</h3>
        <p>The gap is <code>$1 - (sum of every outcome's ask price)</code>. It
        exists only when the outcomes' implied probabilities add up to under
        100%. In practice these gaps are tiny, 1 to 3 cents, and live for well
        under a second before someone closes them.</p>
        <div class="eg"><b>Worked example.</b> YES asks at 0.62, NO asks at
        0.35. Ask sum = 0.97, so the raw gap is 3¢ per share. Buy one of each for
        $0.97; the set pays $1 at resolution. Before fees, that is 3¢ of profit
        on every share you can fill on both legs.</div>
      </section>

      <section class="concept c3">
        <h3><span class="num">3</span>Why fees decide everything</h3>
        <p>The taker fee follows <code>fee = shares x rate x p x (1 - p)</code>.
        That <code>p x (1 - p)</code> term peaks at p = 0.50, which is exactly
        where most gaps cluster, so fees bite hardest precisely where the
        opportunities are.</p>
        <div class="eg"><b>Same gap, two venues.</b> Take the 3¢ gap near
        p = 0.5, where <code>p x (1 - p)</code> is about 0.25.
        <br><span class="good">Sports, rate 0.03:</span> fee is about
        0.03 x 0.25 = 0.75¢ across both legs, so roughly 2¢ of net edge
        survives. A trade.
        <br><span class="bad">Crypto, rate 0.07:</span> fee is about
        0.07 x 0.25 = 1.75¢, which nearly eats the whole 3¢. A trap.
        <br>Identical gap on screen; the fee schedule decides.</div>
      </section>

      <section class="concept c4 wide">
        <h3><span class="num">4</span>Taker vs maker</h3>
        <p>There are two ways to fill the legs of an arb.</p>
        <p><b>Taker.</b> You cross the spread right now and pay the current ask
        plus the taker fee. You fill immediately, which is what you want when a
        gap is about to vanish. This dashboard models the taker.</p>
        <p><b>Maker.</b> You rest a limit order below the ask and wait for
        someone to trade into you. If it fills you get a better price (sometimes
        a rebate), but you are last in a FIFO queue, you suffer adverse
        selection (you tend to fill exactly when the price is moving against
        you), and you carry leg risk.</p>
        <div class="eg"><b>Maker example.</b> YES asks at 0.62, NO asks at 0.35.
        Instead of taking both, you rest a NO bid at 0.34 hoping to shave a cent.
        The YES leg fills at 0.62. But your 0.34 NO never trades, and the market
        drifts: NO now asks 0.45. You are holding a bare YES position, no longer
        hedged.</div>
        <div class="eg warn"><b>That is leg risk.</b> An arb needs every leg
        filled. With one leg on, you are a naked directional bet. If the price
        keeps moving you get liquidated at a loss that can dwarf the few cents
        you were chasing. The maker's better price is not free: it is paid for
        in fill uncertainty.</div>
      </section>

      <section class="concept c5 wide">
        <h3><span class="num">5</span>How the bot actually runs</h3>
        <p>The harness watches roughly 400 markets at once, about 300 on
        Polymarket and 100 on Kalshi, ranked by 24 hour volume. It does not scan
        on a slow timer: it reacts to the order books as they move.</p>
        <p><b>Polymarket</b> streams over a websocket, so a book update arrives
        the instant it happens, sub-second. <b>Kalshi</b> has no public push
        feed, so it is polled once per second over REST. Either way, every book
        update is immediately re-checked for a gap, and the taker math (ask sum
        vs $1, minus fees) is recomputed on the fresh prices.</p>
        <div class="eg"><b>Why every trade is Polymarket.</b> Kalshi's public
        API returns only the bid side, so the ask side has to be inferred as
        1 minus the opposing bid. Bids always sit below asks, so the inferred
        YES ask plus NO ask almost always sums to more than $1: no gap. Combined
        with negative-risk multi-outcome groups being a Polymarket feature, the
        real gaps come from Polymarket, and Kalshi mainly serves as a live
        cross-venue reference.</div>
        <div class="eg"><b>When it tries a maker order.</b> On every detected
        gap the harness also rests a limit buy one tick above the best bid on
        each leg, but only if those limit prices would still lock an edge when
        all legs fill. It waits up to 120 seconds; if the gap has not filled by
        then it cancels the unfilled legs. Any leg that did fill gets liquidated
        at market, which is exactly the leg-risk loss you see in the maker chart
        on the Live gaps page.</div>
      </section>

      <section class="concept c6">
        <h3><span class="num">6</span>Cross-venue gaps</h3>
        <p>Sometimes two venues price complementary outcomes of the same event
        so that YES on one plus NO on the other sums to under $1. The
        disagreement between venues is the gap.</p>
        <div class="eg warn"><b>The catch.</b> This only works if both markets
        resolve on identical criteria. If the rules differ even slightly, the
        "arb" is really a hidden bet on the difference, so cross-venue pairs
        must be verified by hand in mappings.yaml, never matched automatically.</div>
      </section>

      <section class="concept c7 grow">
        <h3><span class="num">7</span>Ladder arbitrage</h3>
        <p>A token's price is just the market's probability, so a more likely
        event should always cost more. Crypto lists ladders of threshold markets
        on the same asset and date, like "BTC above $60k", "above $64k", "above
        $68k on July 16". A bigger move is always less likely than a smaller
        one, so as the strike climbs the price must fall. The cheapest rung
        should be the least likely, and the priciest rung the most likely.</p>
        <p>Sometimes the market gets this backwards and a more likely rung
        trades cheaper than a less likely one. That is a mispricing. We buy the
        underpriced likelier rung and pair it against the other so the payout is
        locked whichever way the price lands.</p>
        <div class="eg"><b>Worked example.</b> "BTC above $60k" is trading at
        0.50 while "BTC above $64k" is at 0.55. Being above $64k means you are
        automatically above $60k too, so above $60k is the more likely event and
        should be the pricier one. Here it is cheaper, backwards. Buy the $60k
        YES and the $64k NO for 0.50 + 0.45 = 0.95. Whatever BTC does, that pair
        pays at least $1, so the 5 cent gap is locked risk-free profit.</div>
        <div class="eg warn"><b>Why it is rare.</b> Market makers watch these
        ladders closely, so clean mispricings are small and vanish fast, and the
        crypto fee (the highest on Polymarket) eats the tiny ones. The Ladder
        arb page only books a trade when the gap clears the fees.</div>
      </section>

      <section class="concept c8 wide">
        <h3><span class="num">8</span>Fair value (binary options)</h3>
        <p>A YES share is a bet that pays $1 if an event happens, so its price is
        just the market's probability. A crypto threshold market ("Will BTC be
        above $64k on Friday?") is exactly a <b>binary option</b>: it pays $1 if
        the price finishes above the strike. Options pricing gives an independent
        fair probability from three inputs the market cannot argue with: the live
        spot price, how volatile the asset has been, and how much time is left.</p>
        <p>Black-Scholes turns those into <code>fair = &#934;(d2)</code>. When the
        market's price and this model probability disagree by more than fees, that
        gap is the signal: the market is pricing the event differently than a
        principled options model would.</p>
        <div class="eg"><b>Worked example.</b> BTC spot $64,800, strike $64,000,
        two days to expiry, recent volatility about 60% annualized. The model puts
        the odds of finishing above $64k at roughly 60%. If the YES share trades
        at 0.52, the market is 8 cents too cheap versus the model, a positive
        divergence.</div>
        <div class="eg warn"><b>Not arbitrage.</b> Unlike the gaps above, this is
        a directional, model-dependent bet: the "edge" is only real if the
        volatility estimate is right, and a single outcome can go either way. It is
        higher variance and strictly measurement-only here, no orders. Every
        measurement records the model's hyperparameters so we can see how it
        performs and tune it.</div>
      </section>

    </div>"""
    return PAGE.format(nav=nav("/about"), body=body)


def nav(active):
    def link(href, label):
        cls = "on" if href == active else ""
        return f'<a class="{cls}" href="{href}">{label}</a>'
    return (f'<nav>{link("/", "Live gaps")}{link("/ladders", "Ladder arb")}'
            f'{link("/fair-value", "Fair value")}'
            f'{link("/about", "How it works")}</nav>')


PAGE = """<!doctype html><html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>polyarb</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Fraunces:opsz,wght@9..144,600;9..144,900&family=Space+Grotesk:wght@400;500;600&display=swap" rel="stylesheet">
<style>
  :root {{ --cream:#f4ecd8; --ink:#141210; --line:#d8ccae; --pos:#1f6b3b; --neg:#a8321f; }}
  * {{ box-sizing:border-box; }}
  body {{ margin:0; background:var(--cream); color:var(--ink);
         font-family:'Space Grotesk',sans-serif; padding:44px 5vw 90px; }}
  header h1 {{ font-family:'Fraunces',serif; font-weight:900; color:#000;
              font-size:clamp(34px,5vw,58px); letter-spacing:-.02em; margin:0; }}
  header p {{ margin:.3rem 0 0; color:#6a5f45; font-size:15px; }}
  nav {{ display:flex; gap:8px; margin:26px 0 0; position:relative; }}
  nav a {{ position:relative; z-index:1; font-size:17px; font-weight:600; color:#6a5f45;
          text-decoration:none; padding:11px 20px; border-radius:11px;
          transition:color .18s ease; }}
  nav a:hover {{ color:var(--ink); }}
  nav a.on {{ color:#000; }}
  /* animated underline that wipes in on hover / active */
  nav a::after {{ content:''; position:absolute; left:20px; right:20px; bottom:6px; height:2px;
          background:var(--ink); transform:scaleX(0); transform-origin:left center;
          transition:transform .28s cubic-bezier(.2,.7,.2,1); }}
  nav a:hover::after, nav a.on::after {{ transform:scaleX(1); }}
  /* magnetic rectangle that snaps to the nav button you are about to click */
  #navbox {{ position:absolute; top:0; left:0; z-index:0; border-radius:11px;
          background:rgba(26,22,16,.07); border:1.5px solid rgba(26,22,16,.5);
          pointer-events:none; opacity:0; transition:opacity .16s ease;
          will-change:transform,width,height; }}
  #navbox.show {{ opacity:1; }}
  .cards {{ display:flex; flex-wrap:wrap; gap:16px; margin:30px 0 6px; }}
  .card {{ background:#fbf6e9; border:1px solid var(--line); border-radius:14px;
          padding:16px 20px; min-width:150px; flex:1 1 150px;
          opacity:0; transform:translateY(14px);
          animation:rise .5s cubic-bezier(.2,.7,.2,1) forwards;
          transition:transform .18s ease, box-shadow .18s ease; }}
  .card:hover {{ transform:translateY(-3px); box-shadow:0 8px 20px rgba(60,48,20,.12); }}
  .card:nth-child(1){{animation-delay:.04s}} .card:nth-child(2){{animation-delay:.10s}}
  .card:nth-child(3){{animation-delay:.16s}} .card:nth-child(4){{animation-delay:.22s}}
  .card:nth-child(5){{animation-delay:.28s}}
  .card .k {{ font-size:12px; text-transform:uppercase; letter-spacing:.08em; color:#8a7c58; }}
  .card .k a {{ color:inherit; }}
  .card .v {{ font-family:'Fraunces',serif; font-weight:600; font-size:28px; margin-top:4px; }}
  .cards.total {{ margin-top:26px; }}
  .card.big {{ background:#eef0dc; border-color:var(--line); flex:2 1 260px; cursor:pointer; }}
  .card.big .k {{ color:#8a7c58; }}
  .card.big .v {{ font-size:34px; }}
  a.cardlink {{ text-decoration:none; color:inherit; cursor:pointer; display:block; }}
  a.cardlink:hover {{ box-shadow:0 8px 20px rgba(60,48,20,.12); }}
  /* account-growth popup */
  .modal-back {{ display:none; position:fixed; inset:0; z-index:200;
          background:rgba(20,18,16,.42); align-items:center; justify-content:center;
          padding:5vw; animation:fadein .16s ease; }}
  .modal-back.open {{ display:flex; }}
  .modal-box {{ position:relative; background:var(--cream); border:1px solid var(--line);
          border-radius:18px; padding:28px 30px 34px; width:min(900px,100%);
          max-height:88vh; overflow:auto; box-shadow:0 24px 60px rgba(40,30,10,.3);
          animation:rise .35s cubic-bezier(.2,.7,.2,1) both; }}
  .modal-x {{ position:absolute; top:16px; right:16px; width:30px; height:30px; padding:0;
          display:flex; align-items:center; justify-content:center; cursor:pointer;
          background:#fbf6e9; border:1px solid var(--line); border-radius:50%; color:#8a7c58;
          font-family:'Space Grotesk',sans-serif; font-size:14px; }}
  .modal-x:hover {{ background:#efe4c6; color:var(--ink); }}
  @keyframes fadein {{ from {{ opacity:0; }} to {{ opacity:1; }} }}
  h2 {{ font-family:'Fraunces',serif; font-weight:600; margin:40px 0 8px; font-size:23px; }}
  .hint {{ color:#6a5f45; font-size:14px; max-width:820px; margin:14px 0 16px; line-height:1.5; }}
  .hint a {{ color:var(--pos); }}
  table {{ width:100%; border-collapse:collapse; font-size:14px; background:#fbf6e9;
          border:1px solid var(--line); border-radius:12px; overflow:hidden; }}
  th,td {{ padding:9px 12px; text-align:left; border-bottom:1px solid #ece2c8; }}
  th {{ font-weight:600; font-size:12px; text-transform:uppercase; letter-spacing:.05em;
       color:#8a7c58; background:#f0e7ce; }}
  td.r, th.r {{ text-align:right; font-variant-numeric:tabular-nums; }}
  .sheet .row {{ cursor:pointer; }}
  .sheet .row:hover {{ background:#f0e7ce; }}
  .sheet .row.skiprow {{ opacity:.55; }}
  .sheet .row td.q {{ font-weight:500; max-width:340px; overflow:hidden;
                     text-overflow:ellipsis; white-space:nowrap; }}
  .sheet .tm {{ color:#9a8c66; font-variant-numeric:tabular-nums; white-space:nowrap; }}
  .took {{ color:var(--pos); font-weight:600; font-size:12px; letter-spacing:.04em; }}
  .skip {{ color:#9a8c66; font-size:12px; }}
  .detail {{ display:none; }} .detail.open {{ display:table-row; }}
  .detail td {{ background:#f7efd9; }}
  .dgrid {{ display:grid; grid-template-columns:1.4fr 1fr; gap:26px;
           padding:6px 4px 12px; align-items:start; }}
  .dmath {{ border-left:1px solid #e1d4b2; padding-left:24px; }}
  @media (max-width:720px) {{ .dgrid {{ grid-template-columns:1fr; }}
    .dmath {{ border-left:none; padding-left:4px; }} }}
  .dh {{ font-size:12px; text-transform:uppercase; letter-spacing:.06em; color:#8a7c58; margin:10px 0 6px; }}
  .dcol .dh:first-child {{ margin-top:0; }}
  .mlinks {{ display:flex; flex-direction:column; gap:6px; }}
  .mlink {{ font-weight:500; color:var(--pos); text-decoration:none; }}
  .mlink:hover {{ text-decoration:underline; }}
  .mlink.dead {{ color:#9a8c66; }}
  .mlink .arrow {{ font-size:12px; color:#8a7c58; }}
  .legs {{ display:flex; flex-wrap:wrap; gap:10px; }}
  .leg {{ display:flex; gap:8px; align-items:baseline; background:#e9dfc0; border-radius:9px; padding:6px 12px; }}
  .leg .pr {{ font-family:'Fraunces',serif; font-weight:600; font-variant-numeric:tabular-nums; }}
  .mathline {{ font-size:15px; }} .mathline b {{ font-variant-numeric:tabular-nums; }}
  .cmp {{ display:flex; flex-direction:column; gap:8px; max-width:560px; }}
  .cmprow {{ display:flex; align-items:baseline; gap:12px; padding:9px 12px;
            border:1px solid var(--line); border-radius:10px; background:#fbf6e9; }}
  .cmprow.cheaper {{ border-color:var(--pos); background:#eef5ec; }}
  .cmprow .cq {{ flex:1; font-size:13.5px; }}
  .cmprow .cp {{ font-family:'Fraunces',serif; font-weight:600; font-variant-numeric:tabular-nums; }}
  .cmprow .cp em {{ font-style:normal; font-size:11px; color:#9a8c66; font-weight:400; }}
  .cmprow .ctag {{ font-size:11px; text-transform:uppercase; letter-spacing:.05em; color:#8a7c58; }}
  .cmprow.cheaper .ctag {{ color:var(--pos); font-weight:600; }}
  .cmpnote {{ font-size:12.5px; color:#7a6d4c; margin:8px 0 4px; line-height:1.5; max-width:560px; }}
  .note {{ font-size:12.5px; color:#7a6d4c; margin-top:10px; line-height:1.5; max-width:560px; }}
  .muted {{ color:#9a8c66; }}
  tr:last-child td {{ border-bottom:none; }}
  .pos {{ color:var(--pos); font-weight:600; }} .neg {{ color:var(--neg); font-weight:600; }}
  .empty {{ margin-top:40px; font-size:18px; color:#6a5f45; }}
  /* ---- Maker chart ---- */
  h2.mh {{ font-family:'Fraunces',serif; font-weight:600; font-size:24px; margin:48px 0 6px; }}
  .mchart {{ width:100%; height:auto; background:#fbf6e9; border:1px solid var(--line);
            border-radius:14px; margin:14px 0 6px; padding:6px; }}
  .mchart .axgrid {{ stroke:#e6dabb; stroke-width:1; }}
  .mchart .axzero {{ stroke:#c9b784; stroke-width:1.5; }}
  .mchart .axlbl {{ fill:#9a8c66; font-family:'Space Grotesk',sans-serif; font-size:12px; text-anchor:end; }}
  .mchart .pnlline {{ fill:none; stroke:#8a6d2f; stroke-width:2; }}
  .mchart .trendline {{ stroke:#a8321f; stroke-width:1.5; stroke-dasharray:6 5; opacity:.7; }}
  h2.mh.first {{ margin-top:14px; }}
  .mchart .dunf {{ fill:#b3a681; stroke:#fbf6e9; stroke-width:1.5; }}
  .mchart .dleg {{ fill:var(--neg); stroke:#fbf6e9; stroke-width:1.5; }}
  .mchart .dpos {{ fill:var(--pos); stroke:#fbf6e9; stroke-width:1.5; }}
  .mchart .dneg {{ fill:var(--neg); stroke:#fbf6e9; stroke-width:1.5; }}
  .mchart a {{ cursor:pointer; }}
  .mchart a:hover circle {{ r:6.5; }}
  .lg-unf {{ color:#8a7c58; font-weight:600; }}
  .lg-leg {{ color:var(--neg); font-weight:600; }}
  .lg-pos {{ color:var(--pos); font-weight:600; }}
  .takeaway {{ max-width:820px; margin:16px 0 0; font-size:14.5px; line-height:1.6;
              color:#4a4230; font-style:italic; }}
  @keyframes flash {{ 0% {{ background:#f6e6a8; }} 100% {{ background:transparent; }} }}
  .sheet .row.flash td {{ animation:flash 2s ease; }}
  /* ---- view toggle (Top 20 / Today / All) ---- */
  .viewbar {{ display:flex; gap:8px; margin:6px 0 12px; }}
  .pill {{ font-family:'Space Grotesk',sans-serif; font-size:13px; cursor:pointer;
          background:#fbf6e9; border:1px solid var(--line); border-radius:20px;
          padding:6px 15px; color:#6a5f45; transition:all .15s ease; }}
  .pill:hover {{ color:var(--ink); }}
  .pill.on {{ background:#1a1610; color:#f4ecd8; border-color:#1a1610; }}
  .view-top20 tr.row:not(.is-top20), .view-top20 tr.detail:not(.is-top20) {{ display:none; }}
  .view-today tr.row:not(.is-today), .view-today tr.detail:not(.is-today) {{ display:none; }}
  /* ---- depth walk + execution in the expanded row ---- */
  .leg.deep {{ flex-direction:column; align-items:stretch; gap:6px; min-width:180px; }}
  .legtop {{ display:flex; gap:10px; align-items:baseline; justify-content:space-between; }}
  .tiers {{ display:flex; flex-wrap:wrap; gap:5px; }}
  .tier {{ font-size:12px; background:#f4ecd8; border:1px solid #ddd0b0; border-radius:6px;
          padding:2px 7px; font-variant-numeric:tabular-nums; }}
  .execline {{ font-size:14px; }}
  .makerline {{ font-size:12.5px; color:#7a6d4c; margin-top:5px; }}
  .detail td {{ position:relative; }}
  .collapse {{ position:absolute; top:10px; right:12px; width:26px; height:26px; padding:0;
              display:flex; align-items:center; justify-content:center; line-height:1;
              font-family:'Space Grotesk',sans-serif; font-size:15px; cursor:pointer;
              background:transparent; border:1px solid var(--line); border-radius:50%;
              color:#8a7c58; }}
  .collapse:hover {{ background:#efe4c6; color:var(--ink); }}
  /* ---- How it works: colored, animated concept grid ---- */
  .lead {{ max-width:820px; margin:26px 0 8px; font-size:18px; line-height:1.55;
          color:#4a4230; font-family:'Fraunces',serif; font-weight:600;
          animation:rise .6s ease both; }}
  .grid {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(340px,1fr));
          gap:20px; margin:28px 0 10px; align-items:stretch; }}
  .concept.grow {{ grid-column:2 / -1; }}
  @media (max-width:1080px) {{ .concept.grow {{ grid-column:1 / -1; }} }}
  .concept {{ border:1px solid var(--line); border-radius:16px; padding:22px 24px 24px;
             position:relative; line-height:1.58; font-size:15px; color:#2c2620;
             box-shadow:0 1px 0 rgba(0,0,0,.02); opacity:0; transform:translateY(18px);
             animation:rise .55s cubic-bezier(.2,.7,.2,1) forwards;
             transition:transform .18s ease, box-shadow .18s ease; }}
  .concept:hover {{ transform:translateY(-4px); box-shadow:0 10px 26px rgba(60,48,20,.13); }}
  .concept.wide {{ grid-column:1 / -1; }}
  .concept h3 {{ font-family:'Fraunces',serif; font-weight:600; font-size:20px;
                margin:0 0 12px; display:flex; align-items:center; gap:12px; color:#1a1610; }}
  .concept .num {{ font-family:'Fraunces',serif; font-weight:900; font-size:15px;
                  width:30px; height:30px; flex:none; border-radius:50%;
                  display:flex; align-items:center; justify-content:center;
                  background:#1a1610; color:#f4ecd8; }}
  .concept p {{ margin:.4rem 0 .8rem; }}
  .concept code {{ background:rgba(255,255,255,.6); border:1px solid rgba(0,0,0,.08);
                  border-radius:5px; padding:1px 5px; font-size:13px; }}
  .eg {{ background:rgba(255,255,255,.55); border-radius:11px; padding:13px 16px;
        margin-top:12px; font-size:14px; line-height:1.55; border:1px solid rgba(0,0,0,.05); }}
  .eg.warn {{ background:rgba(168,50,31,.07); border-color:rgba(168,50,31,.2); }}
  .eg .good, .concept .good {{ color:var(--pos); font-weight:600; }}
  .eg .bad, .concept .bad {{ color:var(--neg); font-weight:600; }}
  /* per-card tints + staggered entrance */
  .c1 {{ background:#f7ead1; }} .c2 {{ background:#eef0dc; }}
  .c3 {{ background:#f6e6d6; }} .c4 {{ background:#e9edda; }}
  .c5 {{ background:#eae6d3; }} .c6 {{ background:#f2e7d9; }} .c7 {{ background:#eae7df; }}
  .c8 {{ background:#e7ecdd; }}
  .c1 .num{{background:#b5892f}} .c2 .num{{background:#5f7a3a}}
  .c3 .num{{background:#a8321f}} .c4 .num{{background:#3f6b4a}}
  .c5 .num{{background:#8a6d2f}} .c6 .num{{background:#8a5a2f}} .c7 .num{{background:#4a5a6a}}
  .c8 .num{{background:#5f7a3a}}
  .c1{{animation-delay:.05s}} .c2{{animation-delay:.12s}} .c3{{animation-delay:.19s}}
  .c4{{animation-delay:.26s}} .c5{{animation-delay:.33s}} .c6{{animation-delay:.40s}}
  .c7{{animation-delay:.47s}} .c8{{animation-delay:.54s}}
  @keyframes rise {{ to {{ opacity:1; transform:translateY(0); }} }}
  @media (prefers-reduced-motion:reduce) {{
    .lead,.concept,.card {{ animation:none; opacity:1; transform:none; }}
    .concept:hover,.card:hover {{ transform:none; }}
  }}
  footer {{ margin-top:44px; color:#9a8c66; font-size:12px; }}
  #rf {{ position:fixed; top:18px; right:18px; background:#fbf6e9; border:1px solid var(--line);
        border-radius:20px; padding:7px 15px; font-family:'Space Grotesk'; font-size:13px; cursor:pointer; color:#5f5333; }}
  @media (prefers-reduced-motion:reduce) {{
    #navbox {{ display:none; }} nav a::after {{ transition:none; }}
  }}
</style></head><body>
<button id="rf" onclick="location.reload()">refresh</button>
<header><h1>polyarb</h1>
<p>Measurement-first harness for prediction-market arbitrage.</p></header>
{nav}
{body}
<footer>Reads live.sqlite read-only.</footer>
<script>
  function tog(i){{ var e=document.getElementById('d'+i); if(e) e.classList.toggle('open'); }}
  function setView(v){{ var tb=document.getElementById('tb'); if(!tb) return;
    tb.className='view-'+v;
    var ps=document.querySelectorAll('#viewbar .pill');
    for(var k=0;k<ps.length;k++) ps[k].classList.toggle('on', ps[k].dataset.v===v); }}
  function flash(i){{ var r=document.getElementById('row'+i); if(!r) return;
    // If the target is hidden under the current filter, switch to All so the
    // chart click always lands on a visible row.
    if(r.offsetParent===null) setView('all');
    r.scrollIntoView({{behavior:'smooth', block:'center'}});
    r.classList.remove('flash'); void r.offsetWidth; r.classList.add('flash'); }}
  function openAcct(){{ var m=document.getElementById('acctmodal'); if(m) m.classList.add('open'); }}
  function closeAcct(){{ var m=document.getElementById('acctmodal'); if(m) m.classList.remove('open'); }}
  addEventListener('keydown', function(e){{ if(e.key==='Escape') closeAcct(); }});
  // Auto-refresh pauses while a row detail OR the account popup is open.
  setInterval(function(){{ if(!document.querySelector('.detail.open') &&
    !document.querySelector('.modal-back.open')) location.reload(); }}, 30000);

  // ---- Magnetic nav rectangle: a spring-followed box that snaps to whichever
  // nav button you are about to click, so the effect only appears over the nav.
  (function(){{
    if(window.matchMedia('(prefers-reduced-motion:reduce)').matches) return;
    var navEl=document.querySelector('nav');
    if(!navEl) return;
    var box=document.createElement('div'); box.id='navbox';
    navEl.appendChild(box);
    var links=navEl.querySelectorAll('a');
    // spring state for x, y, width, height (relative to the nav element)
    var s={{x:0,y:0,w:0,h:0}}, v={{x:0,y:0,w:0,h:0}}, target=null, active=false;
    function place(el){{
      var n=navEl.getBoundingClientRect(), r=el.getBoundingClientRect();
      target={{x:r.left-n.left, y:r.top-n.top, w:r.width, h:r.height}};
    }}
    links.forEach(function(a){{
      a.addEventListener('pointerenter', function(){{
        if(!active){{ place(a); s.x=target.x; s.y=target.y; s.w=target.w; s.h=target.h; }}
        else place(a);
        active=true; box.classList.add('show');
      }});
    }});
    navEl.addEventListener('pointerleave', function(){{ active=false; box.classList.remove('show'); }});
    function spring(p,vel,t,k,d){{ var f=(t-p)*k; vel=(vel+f)*d; return [p+vel,vel]; }}
    function frame(){{
      if(active && target){{
        var a=spring(s.x,v.x,target.x,0.28,0.62); s.x=a[0]; v.x=a[1];
        var b=spring(s.y,v.y,target.y,0.28,0.62); s.y=b[0]; v.y=b[1];
        var c=spring(s.w,v.w,target.w,0.28,0.62); s.w=c[0]; v.w=c[1];
        var e=spring(s.h,v.h,target.h,0.28,0.62); s.h=e[0]; v.h=e[1];
        box.style.width=s.w+'px'; box.style.height=s.h+'px';
        box.style.transform='translate('+s.x+'px,'+s.y+'px)';
      }}
      requestAnimationFrame(frame);
    }}
    requestAnimationFrame(frame);
  }})();
</script>
</body></html>"""


class H(BaseHTTPRequestHandler):
    db = None

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            html = render(self.db).encode()
        elif self.path == "/ladders":
            html = render_ladders(self.db).encode()
        elif self.path == "/fair-value":
            html = render_fairvalue(self.db).encode()
        elif self.path == "/about":
            html = render_about().encode()
        else:
            self.send_response(404); self.end_headers(); return
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(html)))
        self.end_headers()
        self.wfile.write(html)

    def log_message(self, *a):
        pass


def serve(db: str, port: int = 8787) -> None:
    H.db = db
    print(f"polyarb dashboard -> http://127.0.0.1:{port}  (db={db})")
    HTTPServer(("127.0.0.1", port), H).serve_forever()


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="/Users/shrishtiroy/polyarb-data/live.sqlite")
    ap.add_argument("--port", type=int, default=8787)
    a = ap.parse_args()
    serve(a.db, a.port)
