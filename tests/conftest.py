from __future__ import annotations

import pytest

from polyarb.config import Config
from polyarb.models import (
    BookLevel,
    Category,
    Market,
    OrderBook,
    Outcome,
    Venue,
)


def make_market(venue=Venue.POLYMARKET, market_id="m1", category=Category.SPORTS,
                yes_id="yes1", no_id="no1", taker_rate=None) -> Market:
    return Market(
        venue=venue, market_id=market_id, question="test?", category=category,
        outcomes=[Outcome(outcome_id=yes_id, name="Yes"),
                  Outcome(outcome_id=no_id, name="No")],
        taker_rate=taker_rate,
    )


def make_book(venue, market_id, outcome_id, bids, asks, ts=1000.0) -> OrderBook:
    book = OrderBook(
        venue=venue, market_id=market_id, outcome_id=outcome_id, ts=ts,
        bids=[BookLevel(price=p, size=s) for p, s in bids],
        asks=[BookLevel(price=p, size=s) for p, s in asks],
    )
    book.normalize()
    return book


@pytest.fixture
def config() -> Config:
    return Config(
        target_notional=500.0,
        min_gross_edge=0.005,
        min_executable_shares=100.0,
        maker_order_ttl_s=120.0,
    )
