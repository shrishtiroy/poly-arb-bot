"""Pure parsing/normalization functions of each venue adapter."""

import pytest

from polyarb.models import Venue
from polyarb.venues import kalshi, novig, polymarket


# ------------------------------------------------------------------ Polymarket

def test_polymarket_ws_book_parse():
    msg = {
        "event_type": "book", "asset_id": "tok1", "market": "0xc1",
        "timestamp": "1751000000000",
        "buys": [{"price": "0.46", "size": "300"}],
        "sells": [{"price": "0.48", "size": "400"}, {"price": "0.50", "size": "100"}],
    }
    book = polymarket.parse_ws_book(msg)
    assert book.venue == Venue.POLYMARKET
    assert book.best_bid == pytest.approx(0.46)
    assert book.best_ask == pytest.approx(0.48)
    assert book.ts == pytest.approx(1751000000.0)


def test_polymarket_price_change_updates_level():
    book = polymarket.parse_ws_book({
        "event_type": "book", "asset_id": "tok1", "market": "0xc1",
        "timestamp": "1751000000000",
        "buys": [{"price": "0.46", "size": "300"}],
        "sells": [{"price": "0.48", "size": "400"}],
    })
    polymarket.apply_price_change(book, {
        "asset_id": "tok1", "timestamp": "1751000001000",
        "changes": [
            {"price": "0.48", "size": "0", "side": "SELL"},     # level removed
            {"price": "0.47", "size": "200", "side": "BUY"},    # new best bid
        ],
    })
    assert book.best_ask is None
    assert book.best_bid == pytest.approx(0.47)


def test_polymarket_fee_rate_normalization():
    assert polymarket.normalize_fee_rate(700) == pytest.approx(0.07)  # bps
    assert polymarket.normalize_fee_rate(0.07) == pytest.approx(0.07)
    assert polymarket.normalize_fee_rate(0) == 0.0
    assert polymarket.normalize_fee_rate(None) is None


def test_polymarket_clob_market_parse():
    market = polymarket.parse_clob_market({
        "condition_id": "0xc1", "question": "Will X?",
        "taker_base_fee": 300, "maker_base_fee": 0,
        "tokens": [
            {"token_id": "t-yes", "outcome": "Yes"},
            {"token_id": "t-no", "outcome": "No"},
        ],
    }, category=polymarket.map_category("sports"))
    assert market.taker_rate == pytest.approx(0.03)
    assert market.maker_rate == 0.0
    assert market.is_binary


# ---------------------------------------------------------------------- Kalshi

def test_kalshi_orderbook_mirror():
    yes_book, no_book = kalshi.parse_orderbook("TICK", {
        "orderbook": {
            "yes": [[46, 300]],  # resting YES buys at 46c
            "no": [[51, 400]],   # resting NO buys at 51c
        }
    }, ts=1000.0)
    assert yes_book.best_bid == pytest.approx(0.46)
    # NO bid at 51c == YES offered at 49c for the same 400 contracts
    assert yes_book.best_ask == pytest.approx(0.49)
    assert yes_book.asks[0].size == 400
    assert no_book.best_bid == pytest.approx(0.51)
    assert no_book.best_ask == pytest.approx(0.54)


def test_kalshi_trade_parse():
    trade = kalshi.parse_trade({
        "ticker": "TICK", "yes_price": 47, "count": 25,
        "taker_side": "no", "created_time": "2026-07-01T12:00:00Z",
    })
    assert trade.price == pytest.approx(0.47)
    assert trade.side == "sell"  # NO taker == aggressive YES seller
    assert trade.size == 25


# ----------------------------------------------------------------------- Novig

def test_novig_american_odds_normalization():
    assert novig.american_to_prob(150) == pytest.approx(0.4)
    assert novig.american_to_prob(-200) == pytest.approx(2 / 3)
    assert novig.normalize_price(0.4) == pytest.approx(0.4)     # probability
    assert novig.normalize_price(40) == pytest.approx(0.4)      # cents
    assert novig.normalize_price(150) == pytest.approx(0.4)     # american +
    assert novig.normalize_price(-200) == pytest.approx(2 / 3)  # american -


def test_novig_orderbook_parse_both_shapes():
    from_dicts = novig.parse_orderbook("9", "9-home", {
        "bids": [{"price": 0.46, "size": 300}],
        "asks": [{"odds": 120, "quantity": 200}],
    }, ts=1000.0)
    assert from_dicts.best_bid == pytest.approx(0.46)
    assert from_dicts.best_ask == pytest.approx(100 / 220)

    from_pairs = novig.parse_orderbook("9", "9-home", {
        "bids": [[0.45, 100]], "asks": [[0.55, 100]],
    }, ts=1000.0)
    assert from_pairs.best_bid == pytest.approx(0.45)
    assert from_pairs.best_ask == pytest.approx(0.55)


def test_novig_market_is_sports_and_feeless():
    market = novig.parse_market({
        "id": 9, "name": "Team A vs Team B",
        "outcomes": [{"id": "9-home", "name": "Home"},
                     {"id": "9-away", "name": "Away"}],
        "event_id": 77,
    })
    assert market.category.value == "sports"
    assert market.taker_rate is None  # fees.py resolves to 0 for Novig
