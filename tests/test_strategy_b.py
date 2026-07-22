"""Tests for Strategy B: Black-Scholes binary fair-value divergence.

All pure-function, no network (mirrors test_ladders.py / test_adapters.py).
"""

import math

from polyarb.binance import (
    asset_to_symbol,
    parse_closes,
    parse_price,
    parse_symbols,
)
from polyarb.blackscholes import (
    binary_call_prob,
    binary_prob,
    norm_cdf,
    periods_per_year,
    realized_vol,
)
from polyarb.config import Config
from polyarb.models import Category, Market, Outcome, Venue
from polyarb.strategy_b import StrategyB, extract_asset


# --------------------------------------------------------------- normal CDF

def test_norm_cdf_known_values():
    assert norm_cdf(0.0) == 0.5
    assert abs(norm_cdf(1.96) - 0.975) < 1e-3
    assert abs(norm_cdf(-1.96) - 0.025) < 1e-3


# ------------------------------------------------------------ binary_call_prob

def test_at_the_money_is_half_any_vol():
    for sigma in (0.2, 0.6, 1.5):
        assert abs(binary_call_prob(100, 100, sigma, 0.01) - 0.5) < 0.05


def test_deep_itm_and_otm():
    assert binary_call_prob(200, 100, 0.5, 0.05) > 0.9   # far above strike
    assert binary_call_prob(50, 100, 0.5, 0.05) < 0.1    # far below strike


def test_monotonic_in_spot():
    probs = [binary_call_prob(s, 100, 0.6, 0.02) for s in (80, 90, 100, 110, 120)]
    assert probs == sorted(probs)


def test_worked_example_from_theory():
    # S=64800, K=64000, sigma=0.60, T=2/365 -> fair ~ 0.60 (from our walkthrough)
    fair = binary_call_prob(64800, 64000, 0.60, 2 / 365)
    assert abs(fair - 0.60) < 0.03


def test_degenerate_cases():
    assert binary_call_prob(110, 100, 0.5, 0.0) == 1.0   # no time, above
    assert binary_call_prob(90, 100, 0.5, 0.0) == 0.0    # no time, below
    assert binary_call_prob(110, 100, 0.0, 0.02) == 1.0  # no vol, above


# ------------------------------------------------------------------ binary_prob

def test_up_down_symmetry():
    up = binary_prob("up", 105, 100, 0.6, 0.02)
    down = binary_prob("down", 105, 100, 0.6, 0.02)
    assert abs(up + down - 1.0) < 1e-9


# ------------------------------------------------------------------ realized_vol

def test_flat_series_zero_vol():
    assert realized_vol([100.0] * 50, periods_per_year("1m")) == 0.0


def test_realized_vol_positive_and_scales():
    closes = [100, 101, 100, 102, 101, 103, 102]
    vol = realized_vol([float(c) for c in closes], periods_per_year("1m"))
    assert vol > 0


def test_periods_per_year():
    assert periods_per_year("1m") == 365 * 24 * 60
    assert periods_per_year("1h") == 365 * 24
    assert periods_per_year("1d") == 365


# ------------------------------------------------------------- binance parsing

def test_parse_symbols_trading_usdt_only():
    raw = {"symbols": [
        {"symbol": "BTCUSDT", "status": "TRADING", "quoteAsset": "USDT"},
        {"symbol": "ETHUSDT", "status": "TRADING", "quoteAsset": "USDT"},
        {"symbol": "FOOUSDT", "status": "BREAK", "quoteAsset": "USDT"},
        {"symbol": "BTCUSD", "status": "TRADING", "quoteAsset": "USD"},
    ]}
    assert parse_symbols(raw) == {"BTCUSDT", "ETHUSDT"}


def test_parse_price_and_closes():
    assert parse_price({"symbol": "BTCUSDT", "price": "64123.45"}) == 64123.45
    klines = [[0, "1", "2", "0.5", "1.5", "10"], [0, "1.5", "2", "1", "1.8", "5"]]
    assert parse_closes(klines) == [1.5, 1.8]


def test_asset_to_symbol_resolution():
    syms = {"BTCUSDT", "ETHUSDT", "SOLUSDT"}
    assert asset_to_symbol("bitcoin", syms) == "BTCUSDT"
    assert asset_to_symbol("Ethereum", syms) == "ETHUSDT"
    assert asset_to_symbol("solana", syms) == "SOLUSDT"
    assert asset_to_symbol("dogecoin", syms) is None  # not in the live set
    assert asset_to_symbol("", syms) is None


def test_extract_asset():
    assert extract_asset("Will Bitcoin be above $60,000 on July 16?") == "bitcoin"
    assert extract_asset("Will Ethereum reach $3,000 in July?") == "ethereum"
    assert extract_asset("Will the Fed cut rates?") is None


# --------------------------------------------------------------- crypto_target

def _crypto_market(question: str, end_ts=1_800_000_000.0) -> Market:
    return Market(
        venue=Venue.POLYMARKET, market_id="c1", question=question,
        category=Category.CRYPTO, end_ts=end_ts,
        outcomes=[Outcome(outcome_id="c1-y", name="Yes"),
                  Outcome(outcome_id="c1-n", name="No")],
    )


def _strategy_with_symbols(*symbols):
    sb = StrategyB(Config(), {}, recorder=None)
    sb._symbol_set = set(symbols)
    return sb


def test_crypto_target_up_and_down():
    sb = _strategy_with_symbols("BTCUSDT", "ETHUSDT")
    eth = _crypto_market("Will Ethereum be above $3,000 in July?")
    assert sb.crypto_target(eth) == ("ethereum", "ETHUSDT", "up", 3000)
    btc = _crypto_market("Will Bitcoin dip to $55,000 in July?")
    assert sb.crypto_target(btc) == ("bitcoin", "BTCUSDT", "down", 55000)


def test_crypto_target_rejects_non_targets():
    sb = _strategy_with_symbols("BTCUSDT")
    # not a threshold question
    assert sb.crypto_target(_crypto_market("Who wins the election?")) is None
    # asset has no live symbol
    sb2 = _strategy_with_symbols("ETHUSDT")
    assert sb2.crypto_target(_crypto_market("Will Bitcoin be above $60k?")) is None
    # missing expiry
    no_exp = _crypto_market("Will Bitcoin be above $60k?", end_ts=None)
    assert sb.crypto_target(no_exp) is None
