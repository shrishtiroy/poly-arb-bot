"""Venue-agnostic data models.

All prices are probabilities in [0, 1] (i.e. dollars per $1-payout share).
Adapters are responsible for normalizing venue-native units (cents on Kalshi,
American odds on Novig) into this space before anything downstream sees them.
"""

from __future__ import annotations

import time
import uuid
from enum import Enum
from typing import Literal

from pydantic import BaseModel, Field


class Venue(str, Enum):
    POLYMARKET = "polymarket"
    KALSHI = "kalshi"
    NOVIG = "novig"


class Category(str, Enum):
    SPORTS = "sports"
    CRYPTO = "crypto"
    POLITICS = "politics"
    FINANCE = "finance"
    ECONOMICS = "economics"
    GEOPOLITICS = "geopolitics"
    CULTURE = "culture"
    TECH = "tech"
    OTHER = "other"


class Outcome(BaseModel):
    outcome_id: str
    name: str


class Market(BaseModel):
    venue: Venue
    market_id: str
    question: str
    category: Category = Category.OTHER
    outcomes: list[Outcome]
    # Non-null for multi-outcome (negative-risk style) groups: all markets in
    # the group share exactly one winning outcome.
    group_id: str | None = None
    # Fee parameters as reported by the venue API at discovery time. None means
    # "venue did not expose it"; fees.FeeModel then falls back to the published
    # static schedule (with a logged warning).
    taker_rate: float | None = None
    maker_rate: float | None = None

    @property
    def is_binary(self) -> bool:
        return len(self.outcomes) == 2


class BookLevel(BaseModel):
    price: float
    size: float  # shares


class OrderBook(BaseModel):
    """One side-pair book for a single outcome token."""

    venue: Venue
    market_id: str
    outcome_id: str
    ts: float = Field(default_factory=time.time)
    bids: list[BookLevel] = Field(default_factory=list)  # sorted desc by price
    asks: list[BookLevel] = Field(default_factory=list)  # sorted asc by price

    def normalize(self) -> None:
        self.bids.sort(key=lambda l: l.price, reverse=True)
        self.asks.sort(key=lambda l: l.price)

    @property
    def best_bid(self) -> float | None:
        return self.bids[0].price if self.bids else None

    @property
    def best_ask(self) -> float | None:
        return self.asks[0].price if self.asks else None

    def cost_to_buy(self, shares: float) -> tuple[float, float]:
        """Walk the ask side for `shares`. Returns (filled_shares, dollar_cost).

        filled_shares < shares means the book is too thin.
        """
        remaining = shares
        cost = 0.0
        for level in self.asks:
            take = min(remaining, level.size)
            cost += take * level.price
            remaining -= take
            if remaining <= 1e-9:
                break
        return shares - remaining, cost

    def proceeds_to_sell(self, shares: float) -> tuple[float, float]:
        """Walk the bid side for `shares`. Returns (filled_shares, dollar_proceeds)."""
        remaining = shares
        proceeds = 0.0
        for level in self.bids:
            take = min(remaining, level.size)
            proceeds += take * level.price
            remaining -= take
            if remaining <= 1e-9:
                break
        return shares - remaining, proceeds

    def size_at(self, side: Literal["bid", "ask"], price: float) -> float:
        levels = self.bids if side == "bid" else self.asks
        for level in levels:
            if abs(level.price - price) < 1e-9:
                return level.size
        return 0.0


# ---------------------------------------------------------------------------
# Normalized event stream (what adapters emit and what replay files contain)
# ---------------------------------------------------------------------------


class MarketEvent(BaseModel):
    """Market definition announcement (used by replay to seed discovery)."""

    type: Literal["market"] = "market"
    ts: float
    market: Market


class BookEvent(BaseModel):
    """Full snapshot of one outcome's book. Adapters emit snapshots, not deltas;
    per-venue delta handling stays inside each adapter."""

    type: Literal["book"] = "book"
    ts: float
    book: OrderBook


class TradeEvent(BaseModel):
    """A print. `side` is the aggressor side relative to this outcome's book."""

    type: Literal["trade"] = "trade"
    ts: float
    venue: Venue
    market_id: str
    outcome_id: str
    price: float
    size: float
    side: Literal["buy", "sell"]


FeedEvent = MarketEvent | BookEvent | TradeEvent


# ---------------------------------------------------------------------------
# Gaps
# ---------------------------------------------------------------------------


class GapKind(str, Enum):
    BINARY = "binary"          # YES ask + NO ask < 1 on one venue
    MULTI = "multi"            # sum of YES asks across a group < 1
    CROSS_VENUE = "cross_venue"  # YES on venue A + NO on venue B < 1


class GapLeg(BaseModel):
    venue: Venue
    market_id: str
    outcome_id: str
    avg_price: float  # depth-weighted for executable_shares


class GapEvent(BaseModel):
    gap_id: str = Field(default_factory=lambda: uuid.uuid4().hex)
    kind: GapKind
    ts: float
    category: Category
    legs: list[GapLeg]
    executable_shares: float   # shares fillable on EVERY leg simultaneously
    gross_edge_per_share: float  # 1 - sum(leg avg prices), before fees
    taker_fees_per_share: float  # sum of taker fees across legs, per share
    net_taker_edge_per_share: float


# ---------------------------------------------------------------------------
# Paper trading
# ---------------------------------------------------------------------------


class Policy(str, Enum):
    TAKER = "taker"
    MAKER = "maker"


class PaperOrder(BaseModel):
    order_id: str = Field(default_factory=lambda: uuid.uuid4().hex)
    gap_id: str
    policy: Policy
    venue: Venue
    market_id: str
    outcome_id: str
    category: Category
    price: float
    size: float
    ts_placed: float
    # Pessimistic FIFO model: visible size resting at our price when we joined.
    queue_ahead: float = 0.0
    filled: float = 0.0

    @property
    def open_size(self) -> float:
        return self.size - self.filled


class PaperFill(BaseModel):
    order_id: str
    gap_id: str
    policy: Policy
    venue: Venue
    market_id: str
    outcome_id: str
    category: Category
    price: float
    size: float
    fee: float
    rebate_est: float
    ts: float
    note: str = ""


class GapOutcomeKind(str, Enum):
    CAPTURED = "captured"        # all legs filled: locked P&L
    LEG_RISK = "leg_risk"        # partial legs filled, liquidated at market
    UNFILLED = "unfilled"        # no leg filled before expiry/close
    THIN = "thin"                # detected but below min executable depth


class GapResult(BaseModel):
    """Terminal accounting record for one (gap, policy) attempt."""

    gap_id: str
    kind: GapKind
    policy: Policy
    venue: Venue  # primary venue (first leg's venue; xvenue rows use first leg)
    category: Category
    outcome: GapOutcomeKind
    shares: float
    gross_pnl: float      # before fees/rebates
    fees: float
    rebate_est: float
    net_pnl: float        # gross - fees (rebate reported separately)
    ts_open: float
    ts_close: float

    @property
    def lifetime_s(self) -> float:
        return self.ts_close - self.ts_open
