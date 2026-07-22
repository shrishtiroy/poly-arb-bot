"""Novig adapter (sports-only, commission-free P2P exchange).

Auth: OAuth 2.0 client-credentials against the developer API described at
docs.novig.com. Credentials come from env (NOVIG_CLIENT_ID / NOVIG_CLIENT_SECRET)
and are used for read-only market data only - this adapter, like the others,
has no order-placement path.

ENDPOINT CAVEAT: docs.novig.com is unreachable from the build sandbox (egress
proxy 403), so the concrete paths below are configurable via env and must be
confirmed against the live docs on first real run:
    NOVIG_API_BASE   (default https://api.novig.com)
    NOVIG_TOKEN_URL  (default {base}/oauth/token)
    NOVIG_MARKETS_PATH (default /v1/markets)
    NOVIG_ORDERBOOK_PATH (default /v1/markets/{market_id}/orderbook)
The response *parsing* below is pure and fixture-tested, and tolerates both
probability prices and American odds (auto-detected and normalized).
"""

from __future__ import annotations

import asyncio
import logging
import os
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


def american_to_prob(odds: float) -> float:
    """American odds → implied probability. +150 → 0.4, -200 → 2/3."""
    if odds > 0:
        return 100.0 / (odds + 100.0)
    return -odds / (-odds + 100.0)


def normalize_price(raw: float) -> float:
    """Accept probability (0..1), cents (1..99), or American odds (|x| >= 100)."""
    value = float(raw)
    if 0.0 < value < 1.0:
        return value
    if 1.0 <= abs(value) < 100.0:
        return value / 100.0
    return american_to_prob(value)


def parse_market(raw: dict) -> Market | None:
    market_id = str(raw.get("id") or raw.get("market_id") or "")
    outcomes_raw = raw.get("outcomes") or raw.get("selections") or []
    if not market_id or len(outcomes_raw) < 2:
        return None
    return Market(
        venue=Venue.NOVIG,
        market_id=market_id,
        question=raw.get("name") or raw.get("title") or raw.get("question", ""),
        category=Category.SPORTS,  # Novig lists sports only
        outcomes=[
            Outcome(
                outcome_id=str(o.get("id") or o.get("outcome_id")),
                name=o.get("name", ""),
            )
            for o in outcomes_raw
            if o.get("id") or o.get("outcome_id")
        ],
        group_id=str(raw.get("event_id")) if raw.get("event_id") else None,
        taker_rate=raw.get("taker_fee_rate"),  # expected absent (no-vig)
        maker_rate=raw.get("maker_fee_rate"),
    )


def parse_orderbook(market_id: str, outcome_id: str, raw: dict,
                    ts: float | None = None) -> OrderBook:
    """Normalize an orderbook payload with bids/asks arrays of
    {price|odds, size|quantity} objects (or [price, size] pairs)."""

    def levels(rows) -> list[BookLevel]:
        out = []
        for row in rows or []:
            if isinstance(row, dict):
                price = row.get("price", row.get("odds"))
                size = row.get("size", row.get("quantity", 0))
            else:
                price, size = row[0], row[1]
            if price is None or not size:
                continue
            out.append(BookLevel(price=normalize_price(price), size=float(size)))
        return out

    book = OrderBook(
        venue=Venue.NOVIG,
        market_id=market_id,
        outcome_id=outcome_id,
        ts=ts or time.time(),
        bids=levels(raw.get("bids") or raw.get("backs")),
        asks=levels(raw.get("asks") or raw.get("lays")),
    )
    book.normalize()
    return book


def parse_trade(raw: dict) -> TradeEvent | None:
    market_id = str(raw.get("market_id") or "")
    outcome_id = str(raw.get("outcome_id") or "")
    if not market_id or not outcome_id:
        return None
    return TradeEvent(
        ts=float(raw.get("ts") or time.time()),
        venue=Venue.NOVIG,
        market_id=market_id,
        outcome_id=outcome_id,
        price=normalize_price(raw.get("price", 0.5)),
        size=float(raw.get("size", 0)),
        side="buy" if str(raw.get("side", "buy")).lower() == "buy" else "sell",
    )


class NovigAdapter(VenueAdapter):
    venue = Venue.NOVIG

    def __init__(self, config):
        super().__init__(config)
        self.base = os.getenv("NOVIG_API_BASE", "https://api.novig.com").rstrip("/")
        self.token_url = os.getenv("NOVIG_TOKEN_URL", f"{self.base}/oauth/token")
        self.markets_path = os.getenv("NOVIG_MARKETS_PATH", "/v1/markets")
        self.orderbook_path = os.getenv(
            "NOVIG_ORDERBOOK_PATH", "/v1/markets/{market_id}/orderbook"
        )
        self._token: str | None = None
        self._token_expiry: float = 0.0

    async def _auth_headers(self, client: httpx.AsyncClient) -> dict[str, str]:
        client_id = os.getenv("NOVIG_CLIENT_ID")
        client_secret = os.getenv("NOVIG_CLIENT_SECRET")
        if not client_id or not client_secret:
            raise RuntimeError(
                "Novig requires NOVIG_CLIENT_ID / NOVIG_CLIENT_SECRET env vars "
                "(read-only OAuth client credentials; see docs.novig.com). "
                "Run with venues restricted (--config) to skip Novig."
            )
        if self._token is None or time.time() > self._token_expiry - 60:
            resp = await client.post(
                self.token_url,
                data={
                    "grant_type": "client_credentials",
                    "client_id": client_id,
                    "client_secret": client_secret,
                },
            )
            resp.raise_for_status()
            payload = resp.json()
            self._token = payload["access_token"]
            self._token_expiry = time.time() + float(payload.get("expires_in", 1800))
        return {"Authorization": f"Bearer {self._token}"}

    async def discover_markets(self) -> list[Market]:
        markets: list[Market] = []
        async with httpx.AsyncClient(timeout=30) as client:
            headers = await self._auth_headers(client)
            resp = await client.get(
                f"{self.base}{self.markets_path}",
                params={"status": "open", "limit": self.config.max_markets_per_venue},
                headers=headers,
            )
            resp.raise_for_status()
            payload = resp.json()
            rows = payload if isinstance(payload, list) else payload.get("markets", [])
            for raw in rows:
                market = parse_market(raw)
                if market is not None:
                    markets.append(market)
        markets = markets[: self.config.max_markets_per_venue]
        log.info("novig: discovered %d markets", len(markets))
        return markets

    async def stream_events(self, markets: list[Market]) -> AsyncIterator[FeedEvent]:
        async with httpx.AsyncClient(timeout=30) as client:
            while True:
                headers = await self._auth_headers(client)
                for market in markets:
                    url = f"{self.base}" + self.orderbook_path.format(
                        market_id=market.market_id
                    )
                    try:
                        resp = await client.get(url, headers=headers)
                        if resp.status_code != 200:
                            continue
                        payload = resp.json()
                        # Either one book per outcome keyed by outcome id, or a
                        # flat book for outcome[0] with mirrors handled upstream.
                        per_outcome = payload.get("outcomes") if isinstance(payload, dict) else None
                        if isinstance(per_outcome, dict):
                            for outcome in market.outcomes:
                                raw = per_outcome.get(outcome.outcome_id)
                                if raw:
                                    book = parse_orderbook(
                                        market.market_id, outcome.outcome_id, raw
                                    )
                                    yield BookEvent(ts=book.ts, book=book)
                        else:
                            book = parse_orderbook(
                                market.market_id, market.outcomes[0].outcome_id, payload
                            )
                            yield BookEvent(ts=book.ts, book=book)
                    except httpx.HTTPError as exc:
                        log.warning("novig orderbook poll failed for %s: %s",
                                    market.market_id, exc)
                await asyncio.sleep(self.config.novig_poll_interval_s)
