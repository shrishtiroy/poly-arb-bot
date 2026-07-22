"""Polymarket adapter.

Discovery: Gamma API (category/tags) + CLOB REST (token ids, fee rates).
Streaming: public CLOB websocket market channel - no auth needed for market
data. Fee rates are read from the CLOB market object (`taker_base_fee` /
`maker_base_fee`, reported in basis points) so a schedule change shows up
without a code change.

NOTE: the official trading client is `py-clob-client`. We deliberately do not
use it (nor the scam-adjacent `py_clob_client_v2` floating around X threads):
phase 0 needs no keys and places no orders.
"""

from __future__ import annotations

import asyncio
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


def parse_end_ts(raw: str | None) -> float | None:
    """Parse a Gamma ISO 8601 endDate (e.g. "2026-07-16T12:00:00Z") to a unix
    timestamp. Returns None for missing or unparseable values."""
    if not raw:
        return None
    try:
        import datetime
        text = raw.strip().replace("Z", "+00:00")
        return datetime.datetime.fromisoformat(text).timestamp()
    except (ValueError, TypeError):
        return None


def parse_clob_market(raw: dict, category: Category,
                      group_size: int | None = None,
                      slug: str | None = None,
                      event_slug: str | None = None,
                      end_ts: float | None = None) -> Market | None:
    """Normalize one CLOB /markets entry. Returns None for non-tradable rows.

    group_size, when known, is the true number of outcomes in this market's
    neg-risk group (from Gamma /events); the gap detector uses it to reject
    incomplete MULTI partitions.

    slug and event_slug come from the Gamma row (not the CLOB object) and let
    the dashboard link a gap back to its live market page. end_ts is the
    market's expiry (unix seconds) from the Gamma endDate, needed for options
    fair-value pricing.
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
        slug=slug,
        event_slug=event_slug,
        end_ts=end_ts,
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


async def _get_with_retry(client: httpx.AsyncClient, url: str,
                          params: dict | None = None, attempts: int = 4,
                          backoff: float = 1.5) -> httpx.Response:
    """GET with retries on transient transport errors.

    Some networks intercept these hosts with a content filter that flaps on a
    scale of seconds, so a single request can fail with an SSL/connect error
    even though the endpoint is fine moments later. Discovery fires hundreds of
    requests in a burst, so without per-request retries one blip aborts the
    whole venue. Retry transport-level failures with exponential backoff; let
    real HTTP status codes through to the caller unchanged.
    """
    last_exc: Exception | None = None
    for i in range(attempts):
        try:
            return await client.get(url, params=params)
        except (httpx.TransportError, httpx.HTTPError) as exc:
            last_exc = exc
            if i < attempts - 1:
                await asyncio.sleep(backoff * (2 ** i))
    raise last_exc  # exhausted retries


class PolymarketAdapter(VenueAdapter):
    venue = Venue.POLYMARKET

    async def discover_markets(self) -> list[Market]:
        limit = self.config.cap_for(self.venue)
        markets: list[Market] = []
        seen: set[str] = set()  # condition_ids already added
        group_sizes: dict[str, int] = {}  # event id → true # of group members
        async with httpx.AsyncClient(timeout=30) as client:
            # Gamma caps each page at 100 rows regardless of `limit`, so page
            # through with `offset` (volume-ordered) until we hit our cap.
            offset = 0
            while len(markets) < limit:
                resp = await _get_with_retry(
                    client, f"{GAMMA_BASE}/markets",
                    params={
                        "active": "true", "closed": "false",
                        "order": "volume24hr", "ascending": "false",
                        "limit": 100, "offset": offset,
                    },
                )
                resp.raise_for_status()
                gamma_rows = resp.json()
                if not gamma_rows:
                    break  # ran out of active markets before reaching the cap
                offset += len(gamma_rows)
                for row in gamma_rows:
                    market = await self._row_to_market(client, row, group_sizes,
                                                       seen)
                    if market is not None:
                        markets.append(market)
                    if len(markets) >= limit:
                        break
            log.info("polymarket: discovered %d markets (volume-ranked)",
                     len(markets))
            # Ladder arb needs a DIVERSE set of threshold events, which the
            # volume cap alone does not guarantee (low-volume rungs get sliced
            # off, leaving one asset). Keep paging specifically for threshold
            # markets until rungs span enough distinct events.
            await self._scan_ladder_markets(client, markets, seen, group_sizes,
                                             offset)
        log.info("polymarket: discovered %d markets total", len(markets))
        return markets

    async def _row_to_market(self, client: httpx.AsyncClient, row: dict,
                             group_sizes: dict[str, int],
                             seen: set[str]) -> Market | None:
        """Convert one Gamma row to a Market (fetching the CLOB object for token
        ids + fees). Returns None for non-tradable, unfetchable, or duplicate
        rows. Dedupes via `seen` so multiple discovery passes do not double-add."""
        condition_id = row.get("conditionId")
        if not condition_id or condition_id in seen:
            return None
        category = map_category(row.get("category"))
        # For neg-risk markets, look up the group's true member count so the
        # detector can reject partitions sliced by the cap.
        group_size = await self._group_size(client, row, group_sizes)
        # Slugs (from the Gamma row) let the dashboard link back to the live
        # market page; the CLOB object does not carry them.
        slug = row.get("slug") or None
        events = row.get("events") or []
        event_slug = events[0].get("slug") if events else None
        # endDate (ISO 8601) drives time-to-expiry for fair value.
        end_ts = parse_end_ts(row.get("endDate"))
        # CLOB market object carries token ids and live fee rates. A single
        # market that will not fetch (even after retries) is skipped, not fatal:
        # one bad row must not sink discovery.
        try:
            clob_resp = await _get_with_retry(
                client, f"{CLOB_BASE}/markets/{condition_id}")
        except httpx.HTTPError:
            return None
        if clob_resp.status_code != 200:
            return None
        market = parse_clob_market(clob_resp.json(), category, group_size,
                                   slug=slug, event_slug=event_slug,
                                   end_ts=end_ts)
        if market is not None:
            seen.add(condition_id)
        return market

    async def _scan_ladder_markets(self, client: httpx.AsyncClient,
                                   markets: list[Market], seen: set[str],
                                   group_sizes: dict[str, int],
                                   offset: int) -> None:
        """Page further through Gamma, adding only threshold ("above/below/hit
        $X") markets, until their rungs span `ladder_min_events` distinct events
        (or the page budget runs out). This diversifies the ladder universe so
        it is not dominated by whatever high-volume asset the main cap kept."""
        from ..ladders import parse_threshold

        def ladder_events() -> set[str]:
            # Events that currently have >=2 threshold rungs of the same family:
            # only those can actually form a ladder partition.
            rungs: dict[tuple, int] = {}
            for m in markets:
                if not m.is_binary or not m.event_slug:
                    continue
                parsed = parse_threshold(m.question)
                if parsed is None:
                    continue
                rungs[(m.event_slug, parsed[0])] = rungs.get(
                    (m.event_slug, parsed[0]), 0) + 1
            return {ev for (ev, _fam), n in rungs.items() if n >= 2}

        target = self.config.ladder_min_events
        added = 0
        for _ in range(self.config.ladder_scan_max_pages):
            if len(ladder_events()) >= target:
                break
            resp = await _get_with_retry(
                client, f"{GAMMA_BASE}/markets",
                params={
                    "active": "true", "closed": "false",
                    "order": "volume24hr", "ascending": "false",
                    "limit": 100, "offset": offset,
                },
            )
            resp.raise_for_status()
            gamma_rows = resp.json()
            if not gamma_rows:
                break
            offset += len(gamma_rows)
            for row in gamma_rows:
                # Cheap pre-filter on the Gamma question before spending a CLOB
                # fetch: only threshold-parseable rows can be ladder rungs.
                if parse_threshold(row.get("question", "")) is None:
                    continue
                market = await self._row_to_market(client, row, group_sizes, seen)
                if market is not None:
                    markets.append(market)
                    added += 1
        log.info("polymarket: ladder scan added %d threshold markets across "
                 "%d ladderable events", added, len(ladder_events()))

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
                resp = await _get_with_retry(client, f"{GAMMA_BASE}/events/{event_id}")
                resp.raise_for_status()
                cache[event_id] = len(resp.json().get("markets") or [])
            except (httpx.HTTPError, ValueError) as exc:
                log.warning("polymarket: event %s size lookup failed: %s",
                            event_id, exc)
                cache[event_id] = 0  # unknown → treat as no constraint
        return cache[event_id] or None

    async def stream_events(self, markets: list[Market]) -> AsyncIterator[FeedEvent]:
        import asyncio

        asset_ids = [o.outcome_id for m in markets for o in m.outcomes]
        books: dict[str, OrderBook] = {}
        while True:  # reconnect loop
            keepalive = None
            try:
                # max_size=None: the initial book snapshot for hundreds of tokens
                # exceeds the 1 MB default frame limit and would drop the socket
                # (error 1009) on every connect. Prices are trusted venue data.
                # ping_timeout=None: processing that large snapshot plus gap
                # detection can block the event loop past the 20s pong deadline.
                async with websockets.connect(
                    WS_URL, max_size=None, ping_interval=20, ping_timeout=None
                ) as ws:
                    await ws.send(json.dumps({"assets_ids": asset_ids, "type": "market"}))

                    async def _keepalive():
                        # Polymarket drops idle sockets ("no close frame") unless
                        # the client sends an application-level PING periodically.
                        while True:
                            await asyncio.sleep(10)
                            await ws.send("PING")

                    keepalive = asyncio.create_task(_keepalive())
                    while True:
                        # Staleness watchdog: a silent-but-open socket (TCP alive,
                        # no data) would block `async for` forever, stalling gap
                        # detection with no error. If nothing arrives within the
                        # timeout, drop and reconnect.
                        raw = await asyncio.wait_for(ws.recv(), timeout=45)
                        if raw == "PONG":
                            continue
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
            except asyncio.TimeoutError:
                log.warning("polymarket ws stalled (no data for 45s); reconnecting")
            except (websockets.WebSocketException, OSError) as exc:
                log.warning("polymarket ws dropped (%s); reconnecting in 3s", exc)
            finally:
                if keepalive is not None:
                    keepalive.cancel()
            await asyncio.sleep(3)  # backoff before reconnecting


def _as_list(payload) -> list[dict]:
    return payload if isinstance(payload, list) else [payload]
