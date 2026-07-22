"""Binance.US market-data source for Strategy B (spot + realized volatility).

The main Binance API is geo-blocked from this machine (HTTP 451); Binance.US
(`api.binance.us`) returns the identical symbol names (`BTCUSDT`) and
`/api/v3/klines` shape, so it is a drop-in. This module is a thin async fetch
layer plus pure parse helpers and a dynamic asset-name -> symbol resolver, so
coverage is every asset with a live USDT pair, not a hardcoded handful.
"""

from __future__ import annotations

import logging

import httpx

log = logging.getLogger(__name__)

BINANCE_US = "https://api.binance.us"

# Polymarket writes questions in words ("Bitcoin", "Ether"); Binance uses
# tickers. Map the common asset words to their ticker, then confirm the
# <TICKER>USDT pair actually trades before pricing anything. Easy to extend.
_ASSET_ALIASES = {
    "bitcoin": "BTC", "btc": "BTC",
    "ethereum": "ETH", "ether": "ETH", "eth": "ETH",
    "solana": "SOL", "sol": "SOL",
    "xrp": "XRP", "ripple": "XRP",
    "dogecoin": "DOGE", "doge": "DOGE",
    "cardano": "ADA", "ada": "ADA",
    "avalanche": "AVAX", "avax": "AVAX",
    "chainlink": "LINK", "link": "LINK",
    "litecoin": "LTC", "ltc": "LTC",
    "bitcoin cash": "BCH", "bch": "BCH",
    "polkadot": "DOT", "dot": "DOT",
    "shiba inu": "SHIB", "shiba": "SHIB", "shib": "SHIB",
    "polygon": "MATIC", "matic": "MATIC",
    "tron": "TRX", "trx": "TRX",
    "uniswap": "UNI", "uni": "UNI",
    "stellar": "XLM", "xlm": "XLM",
    "cosmos": "ATOM", "atom": "ATOM",
    "aave": "AAVE",
    "near": "NEAR",
    "aptos": "APT", "apt": "APT",
    "arbitrum": "ARB", "arb": "ARB",
    "optimism": "OP",
    "sui": "SUI",
    "pepe": "PEPE",
}


def parse_symbols(raw: dict) -> set[str]:
    """Set of currently-TRADING USDT pairs from an exchangeInfo payload."""
    out: set[str] = set()
    for s in raw.get("symbols", []):
        if (s.get("status") == "TRADING" and s.get("quoteAsset") == "USDT"
                and s.get("symbol")):
            out.add(s["symbol"])
    return out


def parse_price(raw: dict) -> float:
    """Spot price from a /ticker/price payload."""
    return float(raw["price"])


def parse_closes(raw: list) -> list[float]:
    """Close prices (kline index 4) from a /klines payload, oldest first."""
    return [float(k[4]) for k in raw]


def asset_to_symbol(asset_name: str, symbol_set: set[str]) -> str | None:
    """Resolve a Polymarket asset word to a live Binance.US USDT symbol.

    Returns None if the asset is unknown or its pair does not trade, so callers
    skip it rather than invent a price.
    """
    if not asset_name:
        return None
    key = asset_name.strip().lower()
    ticker = _ASSET_ALIASES.get(key)
    if ticker is None:
        # Fall back to treating an all-caps-looking token as its own ticker.
        cand = key.upper()
        if cand.isalpha():
            ticker = cand
    if ticker is None:
        return None
    symbol = f"{ticker}USDT"
    return symbol if symbol in symbol_set else None


class BinanceClient:
    """Thin async wrapper over the public Binance.US REST endpoints."""

    def __init__(self, base: str = BINANCE_US, timeout: float = 15.0):
        self.base = base.rstrip("/")
        self.timeout = timeout
        self._symbols: set[str] | None = None

    async def symbols(self) -> set[str]:
        """Set of TRADING USDT pairs, fetched once and cached."""
        if self._symbols is None:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                resp = await client.get(f"{self.base}/api/v3/exchangeInfo")
                resp.raise_for_status()
                self._symbols = parse_symbols(resp.json())
        return self._symbols

    async def spot(self, symbol: str) -> float:
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.get(f"{self.base}/api/v3/ticker/price",
                                    params={"symbol": symbol})
            resp.raise_for_status()
            return parse_price(resp.json())

    async def recent_closes(self, symbol: str, interval: str,
                            limit: int) -> list[float]:
        # Binance caps klines at 1000 per request; clamp so a big lookback does
        # not silently return a short/empty series.
        limit = max(2, min(limit, 1000))
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.get(
                f"{self.base}/api/v3/klines",
                params={"symbol": symbol, "interval": interval, "limit": limit},
            )
            resp.raise_for_status()
            return parse_closes(resp.json())
