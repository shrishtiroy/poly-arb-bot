"""Fee formulas vs the published schedules (2026-07)."""

import pytest

from conftest import make_market
from polyarb.fees import fee_model_for
from polyarb.models import Category, Venue


def test_polymarket_crypto_peak_fee_matches_published_table():
    # Published: crypto max $1.80 per 100 shares at 50c → 0.07 * 0.25 * 100
    model = fee_model_for(make_market(category=Category.CRYPTO))
    assert model.taker_fee(100, 0.50) == pytest.approx(1.75, abs=0.06)


def test_polymarket_sports_peak_fee():
    # Published: sports max $0.75 per 100 shares at 50c
    model = fee_model_for(make_market(category=Category.SPORTS))
    assert model.taker_fee(100, 0.50) == pytest.approx(0.75)


def test_polymarket_geopolitics_free():
    model = fee_model_for(make_market(category=Category.GEOPOLITICS))
    assert model.taker_fee(100, 0.50) == 0.0


def test_fee_curve_shrinks_at_extremes():
    model = fee_model_for(make_market(category=Category.CRYPTO))
    assert model.taker_fee(100, 0.95) < model.taker_fee(100, 0.50) / 4


def test_polymarket_api_rate_overrides_static_table():
    model = fee_model_for(make_market(category=Category.CRYPTO, taker_rate=0.02))
    assert model.taker_rate == 0.02


def test_polymarket_makers_pay_zero_and_earn_rebate():
    model = fee_model_for(make_market(category=Category.SPORTS))
    assert model.maker_fee(100, 0.50) == 0.0
    # Upper-bound rebate: 25% of the counterparty's 0.75 taker fee
    assert model.maker_rebate_estimate(100, 0.50) == pytest.approx(0.1875)


def test_kalshi_rounds_up_to_cent_per_order():
    model = fee_model_for(make_market(venue=Venue.KALSHI))
    # 1 contract at 50c: 0.07*0.25 = 0.0175 → rounds UP to 2 cents
    assert model.taker_fee(1, 0.50) == pytest.approx(0.02)
    assert model.taker_fee(100, 0.50) == pytest.approx(1.75)
    # No rebate program on Kalshi.
    assert model.maker_rebate_estimate(100, 0.50) == 0.0


def test_novig_is_commission_free():
    model = fee_model_for(make_market(venue=Venue.NOVIG))
    assert model.taker_fee(1000, 0.50) == 0.0
    assert model.maker_fee(1000, 0.50) == 0.0
