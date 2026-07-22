"""Kalshi adapter.

Market data (markets list, orderbook, trades) is served by the public Trade
API v2 REST endpoints without authentication, so live collection needs no
credentials; we poll books at config.kalshi_poll_interval_s. (The websocket
feed requires an RSA API key - a free upgrade path left for a later phase.)

Units: Kalshi prices are integer cents (1..99). The orderbook response lists
resting YES-buy and NO-buy orders; the YES ask side is the mirror of NO bids
(ask_yes = 100 - bid_no), which is how we synthesize two-sided books.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import AsyncIterator

import httpx

from ..models import (
    BookEvent,
    BookLevel,
    Category,
    FeedEvent,
    Market,
    OrderBook,
    Outcome,
    TradeEvent,
    Venue,
)
from .base import VenueAdapter

log = logging.getLogger(__name__)

API_BASE = "https://api.elections.kalshi.com/trade-api/v2"

_CATEGORY_MAP = {
    "sports": Category.SPORTS,
    "crypto": Category.CRYPTO,
    "financials": Category.FINANCE,
    "economics": Category.ECONOMICS,
    "politics": Category.POLITICS,
    "world": Category.GEOPOLITICS,
    "science and technology": Category.TECH,
    "entertainment": Category.CULTURE,
}

YES = "yes"
NO = "no"


def map_category(raw: str | None) -> Category:
    if not raw:
        return Category.OTHER
    return _CATEGORY_MAP.get(raw.strip().lower(), Category.OTHER)


def parse_market(raw: dict) -> Market | None:
    ticker = raw.get("ticker")
    if not ticker:
        return None
    return Market(
        venue=Venue.KALSHI,
        market_id=ticker,
        question=raw.get("title", ""),
        category=map_category(raw.get("category")),
        outcomes=[
            Outcome(outcome_id=f"{ticker}:{YES}", name="Yes"),
            Outcome(outcome_id=f"{ticker}:{NO}", name="No"),
        ],
        group_id=raw.get("event_ticker") or None,
        # Kalshi market pages live at kalshi.com/markets/<event ticker>; store
        # the lowercased ticker so the dashboard can build that link.
        slug=(raw.get("event_ticker") or "").lower() or None,
        # v2 market objects don't expose the fee rate; fees.py falls back to
        # the published 0.07 schedule with a warning.
        taker_rate=None,
        maker_rate=None,
    )


def parse_orderbook(ticker: str, raw: dict, ts: float | None = None) -> list[OrderBook]:
    """Build normalized YES and NO books from a v2 orderbook response.

    Response shape: {"orderbook": {"yes": [[price_cents, contracts], ...],
                                   "no":  [[price_cents, contracts], ...]}}
    where each side lists resting BUY orders for that outcome.
    """
    ts = ts or time.time()
    ob = raw.get("orderbook") or {}
    yes_buys = ob.get("yes") or []
    no_buys = ob.get("no") or []

    def levels(rows) -> list[BookLevel]:
        return [
            BookLevel(price=int(p) / 100.0, size=float(c))
            for p, c in rows
            if c and int(p) > 0
        ]

    yes_bids = levels(yes_buys)
    no_bids = levels(no_buys)
    # Mirror: someone bidding NO at p is offering YES at 1-p for the same size.
    yes_asks = [BookLevel(price=1.0 - l.price, size=l.size) for l in no_bids]
    no_asks = [BookLevel(price=1.0 - l.price, size=l.size) for l in yes_bids]

    books = [
        OrderBook(venue=Venue.KALSHI, market_id=ticker, outcome_id=f"{ticker}:{YES}",
                  ts=ts, bids=yes_bids, asks=yes_asks),
        OrderBook(venue=Venue.KALSHI, market_id=ticker, outcome_id=f"{ticker}:{NO}",
                  ts=ts, bids=no_bids, asks=no_asks),
    ]
    for book in books:
        book.normalize()
    return books


def parse_trade(raw: dict) -> TradeEvent:
    ticker = raw.get("ticker", "")
    taker_side = str(raw.get("taker_side", YES)).lower()
    return TradeEvent(
        ts=_parse_ts(raw.get("created_time")),
        venue=Venue.KALSHI,
        market_id=ticker,
        outcome_id=f"{ticker}:{YES}",
        price=int(raw.get("yes_price", 0)) / 100.0,
        size=float(raw.get("count", 0)),
        side="buy" if taker_side == YES else "sell",
    )


def _parse_ts(value) -> float:
    if value is None:
        return time.time()
    if isinstance(value, (int, float)):
        return float(value)
    from datetime import datetime

    return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()


async def _get_with_retry(client: httpx.AsyncClient, url: str,
                          params: dict | None = None, attempts: int = 4,
                          backoff: float = 1.5) -> httpx.Response:
    """GET with retries on transient transport errors (SSL/connect blips from a
    flapping network content filter). Real HTTP status codes pass through."""
    last_exc: Exception | None = None
    for i in range(attempts):
        try:
            return await client.get(url, params=params)
        except (httpx.TransportError, httpx.HTTPError) as exc:
            last_exc = exc
            if i < attempts - 1:
                await asyncio.sleep(backoff * (2 ** i))
    raise last_exc


class KalshiAdapter(VenueAdapter):
    venue = Venue.KALSHI

    async def discover_markets(self) -> list[Market]:
        cap = self.config.cap_for(self.venue)
        markets: list[Market] = []
        cursor = None
        async with httpx.AsyncClient(timeout=30) as client:
            while len(markets) < cap:
                params = {"status": "open", "limit": 100}
                if cursor:
                    params["cursor"] = cursor
                resp = await _get_with_retry(client, f"{API_BASE}/markets",
                                             params=params)
                resp.raise_for_status()
                payload = resp.json()
                for raw in payload.get("markets", []):
                    market = parse_market(raw)
                    if market is not None:
                        markets.append(market)
                cursor = payload.get("cursor")
                if not cursor:
                    break
        markets = markets[:cap]
        log.info("kalshi: discovered %d markets", len(markets))
        return markets

    async def stream_events(self, markets: list[Market]) -> AsyncIterator[FeedEvent]:
        last_trade_ts: float = time.time()
        async with httpx.AsyncClient(timeout=30) as client:
            while True:
                for market in markets:
                    try:
                        resp = await client.get(
                            f"{API_BASE}/markets/{market.market_id}/orderbook"
                        )
                        if resp.status_code == 200:
                            for book in parse_orderbook(market.market_id, resp.json()):
                                yield BookEvent(ts=book.ts, book=book)
                    except httpx.HTTPError as exc:
                        log.warning("kalshi orderbook poll failed for %s: %s",
                                    market.market_id, exc)
                try:
                    resp = await client.get(
                        f"{API_BASE}/markets/trades",
                        params={"limit": 100, "min_ts": int(last_trade_ts)},
                    )
                    if resp.status_code == 200:
                        for raw in resp.json().get("trades", []):
                            trade = parse_trade(raw)
                            if trade.ts > last_trade_ts:
                                last_trade_ts = trade.ts
                            yield trade
                except httpx.HTTPError as exc:
                    log.warning("kalshi trades poll failed: %s", exc)
                await asyncio.sleep(self.config.kalshi_poll_interval_s)
