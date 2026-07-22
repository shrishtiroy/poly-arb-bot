"""Per-venue fee models.

Every fee-bearing venue currently uses the same bell curve:

    fee = shares * rate * p * (1 - p)

which peaks at p = 0.50 - exactly where arb gaps cluster. Rates are taken from
the venue API at discovery time whenever exposed (Market.taker_rate /
Market.maker_rate); the static tables below are *fallbacks* mirroring the
published schedules as of 2026-07 and log a warning when used, because all
three venues have changed rates before.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass

from .models import Category, Market, Venue

log = logging.getLogger(__name__)

# Polymarket fee structure V2 (effective 2026-03-30). Rate by category.
POLYMARKET_TAKER_RATES: dict[Category, float] = {
    Category.CRYPTO: 0.07,
    Category.ECONOMICS: 0.05,
    Category.CULTURE: 0.05,
    Category.OTHER: 0.05,
    Category.FINANCE: 0.04,
    Category.POLITICS: 0.04,
    Category.TECH: 0.04,
    Category.SPORTS: 0.03,
    Category.GEOPOLITICS: 0.0,
}
# Share of collected taker fees redistributed to makers (20% crypto, ~25% rest).
POLYMARKET_REBATE_SHARE: dict[Category, float] = {
    Category.CRYPTO: 0.20,
}
POLYMARKET_REBATE_SHARE_DEFAULT = 0.25

KALSHI_TAKER_RATE = 0.07
# Kalshi charges makers on designated series only; 0 elsewhere. Per-market
# maker_rate from the API overrides this default.
KALSHI_MAKER_RATE_DEFAULT = 0.0


@dataclass(frozen=True)
class FeeModel:
    venue: Venue
    taker_rate: float
    maker_rate: float
    # Estimated share of the counterparty taker's fee returned to us as a
    # maker rebate. This is an UPPER BOUND: the real pool is pro-rata to maker
    # volume share, which a small account cannot know in advance. The report
    # shows maker P&L both with and without it.
    maker_rebate_share: float
    # Kalshi rounds the fee up to the next cent per order; others don't.
    round_up_cents: bool = False

    def _curve(self, shares: float, price: float, rate: float) -> float:
        fee = shares * rate * price * (1.0 - price)
        if self.round_up_cents and fee > 0:
            # epsilon guards float artifacts (1.75 must not ceil to 1.76)
            fee = math.ceil(fee * 100.0 - 1e-9) / 100.0
        return fee

    def taker_fee(self, shares: float, price: float) -> float:
        return self._curve(shares, price, self.taker_rate)

    def maker_fee(self, shares: float, price: float) -> float:
        return self._curve(shares, price, self.maker_rate)

    def maker_rebate_estimate(self, shares: float, price: float) -> float:
        """Upper-bound rebate if a taker crossed with us for `shares`."""
        counterparty_fee = shares * self.taker_rate * price * (1.0 - price)
        return counterparty_fee * self.maker_rebate_share


def fee_model_for(market: Market) -> FeeModel:
    """Build the fee model for a market, preferring API-reported rates."""
    if market.venue is Venue.POLYMARKET:
        if market.taker_rate is not None:
            taker = market.taker_rate
        else:
            taker = POLYMARKET_TAKER_RATES.get(market.category, 0.05)
            log.warning(
                "polymarket market %s did not expose taker_rate; using static "
                "fallback %.3f for category %s - verify against the live fee schedule",
                market.market_id, taker, market.category.value,
            )
        maker = market.maker_rate if market.maker_rate is not None else 0.0
        rebate = POLYMARKET_REBATE_SHARE.get(
            market.category, POLYMARKET_REBATE_SHARE_DEFAULT
        )
        return FeeModel(Venue.POLYMARKET, taker, maker, rebate)

    if market.venue is Venue.KALSHI:
        if market.taker_rate is not None:
            taker = market.taker_rate
        else:
            taker = KALSHI_TAKER_RATE
            log.warning(
                "kalshi market %s did not expose taker_rate; using static "
                "fallback %.3f - verify against kalshi.com/fee-schedule",
                market.market_id, taker,
            )
        maker = (
            market.maker_rate
            if market.maker_rate is not None
            else KALSHI_MAKER_RATE_DEFAULT
        )
        # Kalshi has no maker rebate program.
        return FeeModel(Venue.KALSHI, taker, maker, 0.0, round_up_cents=True)

    if market.venue is Venue.NOVIG:
        # Commission-free P2P exchange; still honor API-reported overrides so a
        # future fee introduction shows up without a code change.
        return FeeModel(
            Venue.NOVIG,
            market.taker_rate or 0.0,
            market.maker_rate or 0.0,
            0.0,
        )

    raise ValueError(f"unknown venue: {market.venue}")
