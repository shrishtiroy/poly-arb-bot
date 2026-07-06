"""Deterministic synthetic replay generator.

Produces an 8-day, three-venue scenario used by the e2e tests and the demo
replay (`polyarb collect --replay tests/fixtures/replay --config
tests/fixtures/replay/config.yaml`). Six gap episodes per market per day:
four fully print out (maker captures), one gets no prints (unfilled), one
prints a single leg before the gap closes (leg risk).

The numbers are chosen to reproduce the fee asymmetry the harness exists to
measure: a 3-cent gross gap that taker execution keeps on sports (1.5c fees),
loses on crypto (3.5c fees), and keeps fully on fee-free Novig.
"""

from __future__ import annotations

import json
from pathlib import Path

from .models import (
    BookEvent,
    BookLevel,
    Category,
    Market,
    MarketEvent,
    OrderBook,
    Outcome,
    TradeEvent,
    Venue,
)

DAY = 86_400
BASE_TS = 1_751_500_800  # 2026-07-03 00:00 UTC, day-aligned
N_DAYS = 8
EPISODES_PER_DAY = 6
SIZE = 400.0


def _market(venue: Venue, market_id: str, category: Category,
            yes_id: str, no_id: str, taker_rate=None, maker_rate=None) -> Market:
    return Market(
        venue=venue, market_id=market_id, question=f"demo {market_id}",
        category=category,
        outcomes=[Outcome(outcome_id=yes_id, name="Yes"),
                  Outcome(outcome_id=no_id, name="No")],
        taker_rate=taker_rate, maker_rate=maker_rate,
    )


DEMO_MARKETS: list[Market] = [
    _market(Venue.POLYMARKET, "pm-s", Category.SPORTS, "pm-s-yes", "pm-s-no",
            taker_rate=0.03, maker_rate=0.0),
    _market(Venue.POLYMARKET, "pm-c", Category.CRYPTO, "pm-c-yes", "pm-c-no",
            taker_rate=0.07, maker_rate=0.0),
    _market(Venue.KALSHI, "KXS", Category.SPORTS, "KXS:yes", "KXS:no",
            taker_rate=0.07, maker_rate=0.0175),
    _market(Venue.NOVIG, "nv-9", Category.SPORTS, "nv-9-h", "nv-9-a"),
]


def _book(market: Market, outcome_idx: int, bid: float, ask: float,
          ts: float, size: float = 500.0, ask_size: float = SIZE) -> BookEvent:
    outcome = market.outcomes[outcome_idx]
    book = OrderBook(
        venue=market.venue, market_id=market.market_id,
        outcome_id=outcome.outcome_id, ts=ts,
        bids=[BookLevel(price=bid, size=size)],
        asks=[BookLevel(price=ask, size=ask_size)],
    )
    return BookEvent(ts=ts, book=book)


def _trade(market: Market, outcome_idx: int, price: float, ts: float) -> TradeEvent:
    return TradeEvent(
        ts=ts, venue=market.venue, market_id=market.market_id,
        outcome_id=market.outcomes[outcome_idx].outcome_id,
        price=price, size=SIZE, side="sell",
    )


def episode_events(market: Market, t0: float, variant: str) -> list:
    """One gap episode: open (3c gross gap) → prints per variant → close."""
    events = [
        _book(market, 0, bid=0.46, ask=0.48, ts=t0),
        _book(market, 1, bid=0.47, ask=0.49, ts=t0),
    ]
    if variant in ("captured", "leg_risk"):
        events.append(_trade(market, 0, price=0.47, ts=t0 + 5))
    if variant == "captured":
        events.append(_trade(market, 1, price=0.48, ts=t0 + 10))
    # Books tighten: YES+NO asks now sum past $1 → gap closes.
    events.append(_book(market, 0, bid=0.46, ask=0.52, ts=t0 + 20))
    events.append(_book(market, 1, bid=0.47, ask=0.50, ts=t0 + 20))
    return events


VARIANT_CYCLE = ["captured", "captured", "captured", "captured",
                 "unfilled", "leg_risk"]


def generate(out_dir: str | Path) -> Path:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)

    events: list = [MarketEvent(ts=float(BASE_TS - 1), market=m)
                    for m in DEMO_MARKETS]
    for day in range(N_DAYS):
        for k in range(EPISODES_PER_DAY):
            t0 = float(BASE_TS + day * DAY + 3600 * (k + 1))
            variant = VARIANT_CYCLE[k % len(VARIANT_CYCLE)]
            for market in DEMO_MARKETS:
                events.extend(episode_events(market, t0, variant))
    events.sort(key=lambda e: (e.ts, e.type != "market"))

    replay_file = out / "demo.jsonl"
    with open(replay_file, "w") as fh:
        for event in events:
            fh.write(event.model_dump_json() + "\n")

    # Cross-venue mapping: PM sports YES + Kalshi sports NO cover the same
    # (synthetic) event, so their simultaneous episodes create CROSS_VENUE gaps.
    (out / "mappings.yaml").write_text(
        """cross_venue:
  - name: "demo: PM sports YES vs Kalshi sports NO"
    category: sports
    legs:
      - {venue: polymarket, market_id: "pm-s", outcome_id: "pm-s-yes"}
      - {venue: kalshi, market_id: "KXS", outcome_id: "KXS:no"}
"""
    )
    (out / "config.yaml").write_text(
        f"""# demo replay config
db_path: polyarb-demo.sqlite
event_mappings_path: {out / 'mappings.yaml'}
"""
    )
    return replay_file
