"""Strategy B: Black-Scholes binary fair-value divergence (measurement-only).

A Polymarket YES token that pays $1 if a crypto asset finishes above (or below)
a strike at expiry IS a cash-or-nothing binary option. This module values each
such market with Black-Scholes (fair prob = Phi(d2), from live spot + realized
volatility + time-to-expiry) and compares that to the market-implied
probability (the YES token price). The divergence (market_prob - fair_prob) is
the signal.

This is NOT arbitrage: it is a directional, model-dependent bet whose edge only
exists if the volatility estimate is right, so it is strictly measurement-only
(no orders). Every measurement records the hyperparameters that produced it so
the model can be evaluated and tuned later.

Binance REST calls are async but the runner's hot path (process/on_book) is
sync, so a background task keeps a per-symbol spot/vol cache warm and on_book
reads it synchronously (no await, no blocking the event loop).
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass

from . import blackscholes
from .binance import BinanceClient, asset_to_symbol
from .config import Config
from .gaps import yes_outcome_id
from .ladders import parse_threshold
from .models import Market, OrderBook, Venue

log = logging.getLogger(__name__)

YEAR_SECONDS = 365 * 24 * 60 * 60

# Asset words we know how to price, longest first so "bitcoin cash" beats
# "bitcoin". Kept in sync with binance._ASSET_ALIASES.
from .binance import _ASSET_ALIASES  # noqa: E402

_ASSET_WORDS = sorted(_ASSET_ALIASES.keys(), key=len, reverse=True)


def extract_asset(question: str) -> str | None:
    """The crypto asset word mentioned in a question, else None."""
    text = question.lower()
    for word in _ASSET_WORDS:
        if re.search(rf"\b{re.escape(word)}\b", text):
            return word
    return None


@dataclass
class BinaryValuation:
    """One fair-value measurement plus the hyperparameters that produced it."""

    ts: float
    market_id: str
    outcome_id: str
    question: str
    asset: str
    symbol: str
    family: str
    strike: float
    spot: float
    sigma: float
    t_years: float
    fair_prob: float
    market_prob: float
    divergence: float  # market_prob - fair_prob
    # Hyperparameter block (recorded per row for tuning / comparability).
    r: float
    vol_lookback_min: int
    kline_interval: str
    periods_per_year: float
    price_mode: str
    params_version: str


@dataclass
class _Quote:
    spot: float
    sigma: float


class StrategyB:
    """Values crypto threshold markets against a Black-Scholes binary model."""

    def __init__(self, config: Config, market_index: dict, recorder,
                 client: BinanceClient | None = None):
        self.config = config
        self.market_index = market_index  # {(venue, market_id): Market}
        self.recorder = recorder
        self.client = client or BinanceClient(config.binance_base)
        self._quotes: dict[str, _Quote] = {}      # symbol -> latest spot/vol
        self._last_record: dict[str, float] = {}  # market_id -> last record ts
        self._symbol_set: set[str] = set()
        self._targets: dict[str, tuple] = {}      # market_id -> (asset, symbol, family, strike)

    # ---------------------------------------------------------------- targeting

    def crypto_target(self, market: Market) -> tuple | None:
        """Return (asset, symbol, family, strike) if `market` is a binary crypto
        threshold market we can price (parseable strike, resolvable Binance.US
        symbol, known expiry); else None."""
        if not market.is_binary or market.end_ts is None:
            return None
        parsed = parse_threshold(market.question)
        if parsed is None:
            return None
        family, strike = parsed
        asset = extract_asset(market.question)
        if asset is None:
            return None
        symbol = asset_to_symbol(asset, self._symbol_set)
        if symbol is None:
            return None
        return asset, symbol, family, strike

    def resolve_targets(self) -> dict[str, tuple]:
        """Index every priceable crypto market once (requires symbol set)."""
        self._targets = {}
        for (_venue, market_id), market in self.market_index.items():
            target = self.crypto_target(market)
            if target is not None:
                self._targets[market_id] = target
        return self._targets

    def symbols_in_use(self) -> set[str]:
        return {sym for (_a, sym, _f, _s) in self._targets.values()}

    # ------------------------------------------------------------ cache refresh

    async def prime(self) -> None:
        """Load the tradable-symbol set and index targets. Call once at startup
        before the refresh loop (which needs the symbol set to resolve).

        Retries a few times with backoff: a transient DNS/network blip here
        used to zero out every target for the whole multi-hour collector run
        (2026-08-31), since nothing re-attempts this fetch afterwards."""
        for attempt, delay in enumerate((0, 2, 5, 10)):
            if delay:
                await asyncio.sleep(delay)
            try:
                self._symbol_set = await self.client.symbols()
                break
            except Exception as exc:  # noqa: BLE001 - degrade gracefully, no orders anyway
                log.warning("binance exchangeInfo fetch failed (attempt %d): %s",
                            attempt + 1, exc)
                self._symbol_set = set()
        self.resolve_targets()
        log.info("strategy B: %d priceable crypto markets across %d symbols",
                 len(self._targets), len(self.symbols_in_use()))

    async def refresh_spot(self) -> None:
        for symbol in self.symbols_in_use():
            try:
                spot = await self.client.spot(symbol)
            except Exception as exc:  # noqa: BLE001
                log.debug("binance spot %s failed: %s", symbol, exc)
                continue
            prev = self._quotes.get(symbol)
            self._quotes[symbol] = _Quote(spot=spot, sigma=prev.sigma if prev else 0.0)

    async def refresh_vol(self) -> None:
        ppy = blackscholes.periods_per_year(self.config.vol_kline_interval)
        for symbol in self.symbols_in_use():
            try:
                closes = await self.client.recent_closes(
                    symbol, self.config.vol_kline_interval,
                    self.config.vol_lookback_min,
                )
            except Exception as exc:  # noqa: BLE001
                log.debug("binance klines %s failed: %s", symbol, exc)
                continue
            sigma = blackscholes.realized_vol(closes, ppy)
            prev = self._quotes.get(symbol)
            self._quotes[symbol] = _Quote(spot=prev.spot if prev else (closes[-1] if closes else 0.0),
                                          sigma=sigma)

    # --------------------------------------------------------------- hot path

    def _market_prob(self, book: OrderBook) -> float | None:
        """Implied YES probability from the book per the price_mode knob."""
        bid, ask = book.best_bid, book.best_ask
        if self.config.bs_price_mode == "ask":
            return ask
        # mid, with graceful fallback to whichever side exists
        if bid is not None and ask is not None:
            return (bid + ask) / 2.0
        return ask if ask is not None else bid

    def on_book(self, book: OrderBook, ts: float) -> None:
        """Value a crypto target's YES token against the model and record the
        divergence. No-op for non-targets, non-YES tokens, or cold caches."""
        target = self._targets.get(book.market_id)
        if target is None:
            return
        market = self.market_index.get((book.venue, book.market_id))
        if market is None or book.venue is not Venue.POLYMARKET:
            return
        # Only the YES token carries the "above/below strike" probability.
        if book.outcome_id != yes_outcome_id(market):
            return
        asset, symbol, family, strike = target
        quote = self._quotes.get(symbol)
        if quote is None or quote.spot <= 0 or quote.sigma <= 0:
            return  # cache not warm yet
        market_prob = self._market_prob(book)
        if market_prob is None:
            return

        t_years = max((market.end_ts or ts) - ts, 0.0) / YEAR_SECONDS
        fair = blackscholes.binary_prob(family, quote.spot, strike,
                                        quote.sigma, t_years,
                                        self.config.bs_risk_free_rate)
        divergence = market_prob - fair

        # Throttle: skip if too small AND we recorded this market recently.
        last = self._last_record.get(book.market_id, 0.0)
        recent = ts - last < self.config.bs_record_throttle_s
        if abs(divergence) < self.config.strategy_b_min_divergence and recent:
            return
        self._last_record[book.market_id] = ts

        ppy = blackscholes.periods_per_year(self.config.vol_kline_interval)
        self.recorder.record_b_measurement(BinaryValuation(
            ts=ts, market_id=book.market_id, outcome_id=book.outcome_id,
            question=market.question, asset=asset, symbol=symbol,
            family=family, strike=strike, spot=quote.spot, sigma=quote.sigma,
            t_years=t_years, fair_prob=fair, market_prob=market_prob,
            divergence=divergence, r=self.config.bs_risk_free_rate,
            vol_lookback_min=self.config.vol_lookback_min,
            kline_interval=self.config.vol_kline_interval,
            periods_per_year=ppy, price_mode=self.config.bs_price_mode,
            params_version=self.config.bs_params_version,
        ))
