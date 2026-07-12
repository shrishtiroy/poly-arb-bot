"""Polymarket adapter.

Discovery: Gamma API (category/tags) + CLOB REST (token ids, fee rates).
Streaming: public CLOB websocket market channel — no auth needed for market
data. Fee rates are read from the CLOB market object (`taker_base_fee` /
`maker_base_fee`, reported in basis points) so a schedule change shows up
without a code change.

NOTE: the official trading client is `py-clob-client`. We deliberately do not
use it (nor the scam-adjacent `py_clob_client_v2` floating around X threads):
phase 0 needs no keys and places no orders.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import AsyncIterator

import httpx
import websockets

from ..config import Config
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

GAMMA_BASE = "https://gamma-api.polymarket.com"
CLOB_BASE = "https://clob.polymarket.com"
WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"

# Gamma category strings → normalized categories (best-effort; unknown → OTHER)
_CATEGORY_MAP = {
    "sports": Category.SPORTS,
    "crypto": Category.CRYPTO,
    "politics": Category.POLITICS,
    "us-current-affairs": Category.POLITICS,
    "finance": Category.FINANCE,
    "business": Category.FINANCE,
    "economy": Category.ECONOMICS,
    "economics": Category.ECONOMICS,
    "geopolitics": Category.GEOPOLITICS,
    "world": Category.GEOPOLITICS,
    "culture": Category.CULTURE,
    "pop-culture": Category.CULTURE,
    "tech": Category.TECH,
    "science": Category.TECH,
}


def map_category(raw: str | None) -> Category:
    if not raw:
        return Category.OTHER
    return _CATEGORY_MAP.get(raw.strip().lower(), Category.OTHER)


def normalize_fee_rate(raw: float | int | str | None) -> float | None:
    """CLOB fee fields are basis points (e.g. 700 → 0.07). Tolerate either."""
    if raw is None:
        return None
    value = float(raw)
    if value <= 0:
        return 0.0
    return value / 10_000.0 if value > 1.0 else value


def parse_clob_market(raw: dict, category: Category,
                      group_size: int | None = None) -> Market | None:
    """Normalize one CLOB /markets entry. Returns None for non-tradable rows.

    group_size, when known, is the true number of outcomes in this market's
    neg-risk group (from Gamma /events); the gap detector uses it to reject
    incomplete MULTI partitions.
    """
    tokens = raw.get("tokens") or []
    if len(tokens) < 2 or not raw.get("condition_id"):
        return None
    return Market(
        venue=Venue.POLYMARKET,
        market_id=raw["condition_id"],
        question=raw.get("question", ""),
        category=category,
        outcomes=[
            Outcome(outcome_id=t["token_id"], name=t.get("outcome", ""))
            for t in tokens
            if t.get("token_id")
        ],
        group_id=raw.get("neg_risk_market_id") or None,
        group_size=group_size,
        taker_rate=normalize_fee_rate(raw.get("taker_base_fee")),
        maker_rate=normalize_fee_rate(raw.get("maker_base_fee")),
    )


def parse_ws_book(msg: dict) -> OrderBook:
    """`book` event: full snapshot with buys/sells arrays of {price, size} strings."""
    book = OrderBook(
        venue=Venue.POLYMARKET,
        market_id=msg.get("market", ""),
        outcome_id=msg["asset_id"],
        ts=float(msg.get("timestamp", time.time() * 1000)) / 1000.0,
        bids=[BookLevel(price=float(l["price"]), size=float(l["size"]))
              for l in msg.get("buys", msg.get("bids", []))],
        asks=[BookLevel(price=float(l["price"]), size=float(l["size"]))
              for l in msg.get("sells", msg.get("asks", []))],
    )
    book.normalize()
    return book


def parse_ws_trade(msg: dict) -> TradeEvent:
    return TradeEvent(
        ts=float(msg.get("timestamp", time.time() * 1000)) / 1000.0,
        venue=Venue.POLYMARKET,
        market_id=msg.get("market", ""),
        outcome_id=msg["asset_id"],
        price=float(msg["price"]),
        size=float(msg.get("size", 0.0)),
        side="buy" if str(msg.get("side", "BUY")).upper() == "BUY" else "sell",
    )


def apply_price_change(book: OrderBook, msg: dict) -> OrderBook:
    """`price_change` event: level updates; size is the new absolute size."""
    for change in msg.get("changes", msg.get("price_changes", [])):
        price = float(change["price"])
        size = float(change["size"])
        side_levels = book.bids if str(change.get("side", "BUY")).upper() == "BUY" else book.asks
        for i, level in enumerate(side_levels):
            if abs(level.price - price) < 1e-9:
                if size <= 0:
                    side_levels.pop(i)
                else:
                    side_levels[i] = BookLevel(price=price, size=size)
                break
        else:
            if size > 0:
                side_levels.append(BookLevel(price=price, size=size))
    book.normalize()
    book.ts = float(msg.get("timestamp", time.time() * 1000)) / 1000.0
    return book


class PolymarketAdapter(VenueAdapter):
    venue = Venue.POLYMARKET

    async def discover_markets(self) -> list[Market]:
        limit = self.config.max_markets_per_venue
        markets: list[Market] = []
        group_sizes: dict[str, int] = {}  # event id → true # of group members
        async with httpx.AsyncClient(timeout=30) as client:
            # Gamma gives us active markets with category tags and volume order.
            resp = await client.get(
                f"{GAMMA_BASE}/markets",
                params={
                    "active": "true", "closed": "false",
                    "order": "volume24hr", "ascending": "false",
                    "limit": limit,
                },
            )
            resp.raise_for_status()
            gamma_rows = resp.json()
            for row in gamma_rows:
                condition_id = row.get("conditionId")
                if not condition_id:
                    continue
                category = map_category(row.get("category"))
                # For neg-risk markets, look up the group's true member count so
                # the detector can reject partitions sliced by the volume cap.
                group_size = await self._group_size(client, row, group_sizes)
                # CLOB market object carries token ids and live fee rates.
                clob_resp = await client.get(f"{CLOB_BASE}/markets/{condition_id}")
                if clob_resp.status_code != 200:
                    continue
                market = parse_clob_market(clob_resp.json(), category, group_size)
                if market is not None:
                    markets.append(market)
                if len(markets) >= limit:
                    break
        log.info("polymarket: discovered %d markets", len(markets))
        return markets

    async def _group_size(self, client: httpx.AsyncClient, gamma_row: dict,
                          cache: dict[str, int]) -> int | None:
        """True number of markets in this row's neg-risk group, via Gamma
        /events (cached per event). None if the row isn't a neg-risk member."""
        if not gamma_row.get("negRisk"):
            return None
        events = gamma_row.get("events") or []
        event_id = str(events[0].get("id")) if events and events[0].get("id") else None
        if not event_id:
            return None
        if event_id not in cache:
            try:
                resp = await client.get(f"{GAMMA_BASE}/events/{event_id}")
                resp.raise_for_status()
                cache[event_id] = len(resp.json().get("markets") or [])
            except (httpx.HTTPError, ValueError) as exc:
                log.warning("polymarket: event %s size lookup failed: %s",
                            event_id, exc)
                cache[event_id] = 0  # unknown → treat as no constraint
        return cache[event_id] or None

    async def stream_events(self, markets: list[Market]) -> AsyncIterator[FeedEvent]:
        asset_ids = [o.outcome_id for m in markets for o in m.outcomes]
        books: dict[str, OrderBook] = {}
        while True:  # reconnect loop
            try:
                async with websockets.connect(WS_URL) as ws:
                    await ws.send(json.dumps({"assets_ids": asset_ids, "type": "market"}))
                    async for raw in ws:
                        for msg in _as_list(json.loads(raw)):
                            event_type = msg.get("event_type")
                            if event_type == "book":
                                book = parse_ws_book(msg)
                                books[book.outcome_id] = book
                                yield BookEvent(ts=book.ts, book=book)
                            elif event_type == "price_change":
                                book = books.get(msg.get("asset_id", ""))
                                if book is not None:
                                    book = apply_price_change(book, msg)
                                    yield BookEvent(ts=book.ts, book=book.model_copy(deep=True))
                            elif event_type == "last_trade_price":
                                yield parse_ws_trade(msg)
            except (websockets.WebSocketException, OSError) as exc:
                log.warning("polymarket ws dropped (%s); reconnecting in 3s", exc)
                import asyncio

                await asyncio.sleep(3)


def _as_list(payload) -> list[dict]:
    return payload if isinstance(payload, list) else [payload]
