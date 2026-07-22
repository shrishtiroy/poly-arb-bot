"""Black-Scholes binary (digital) option pricing, stdlib only.

A Polymarket YES token that pays $1 if an asset finishes above a strike at
expiry (else $0) IS a cash-or-nothing binary call option. Its fair probability
under Black-Scholes is Phi(d2), the normal CDF of the standardized distance from
spot to strike. We use math.erf for the normal CDF (no numpy/scipy in this repo).

This is a pure math module: no I/O, no state, fully unit-testable.
"""

from __future__ import annotations

import math


def norm_cdf(x: float) -> float:
    """Standard normal cumulative distribution via the error function."""
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def binary_call_prob(spot: float, strike: float, sigma: float, t: float,
                     r: float = 0.0) -> float:
    """Risk-neutral probability an asset finishes ABOVE strike at expiry.

    fair = Phi(d2), d2 = (ln(S/K) + (r - sigma^2/2) t) / (sigma sqrt(t)).
    r (risk-free rate) defaults to 0, which is fine for short-dated crypto, but
    is kept as a tunable hyperparameter. Degenerate cases resolve to the
    intrinsic answer: no time or no volatility means the outcome is already
    decided by whether spot exceeds strike.
    """
    if spot <= 0 or strike <= 0:
        return 0.5
    if t <= 0 or sigma <= 0:
        return 1.0 if spot > strike else 0.0
    d2 = (math.log(spot / strike) + (r - 0.5 * sigma * sigma) * t) / (sigma * math.sqrt(t))
    return norm_cdf(d2)


def binary_prob(family: str, spot: float, strike: float, sigma: float,
                t: float, r: float = 0.0) -> float:
    """Fair probability for an "up" (above strike) or "down" (below strike)
    threshold market. down is the complement of the call probability."""
    up = binary_call_prob(spot, strike, sigma, t, r)
    return up if family == "up" else 1.0 - up


def realized_vol(closes: list[float], periods_per_year: float) -> float:
    """Annualized volatility: standard deviation of log returns over the close
    series, scaled by sqrt(periods_per_year). Returns 0 for flat/short series."""
    rets = []
    for i in range(1, len(closes)):
        prev, cur = closes[i - 1], closes[i]
        if prev > 0 and cur > 0:
            rets.append(math.log(cur / prev))
    n = len(rets)
    if n < 2:
        return 0.0
    mean = sum(rets) / n
    var = sum((x - mean) ** 2 for x in rets) / (n - 1)
    return math.sqrt(var) * math.sqrt(periods_per_year)


# Minutes per year, for annualizing 1-minute-kline realized vol.
MINUTES_PER_YEAR = 365 * 24 * 60  # 525600


def periods_per_year(interval: str) -> float:
    """Annualization factor for a Binance kline interval string."""
    unit = interval[-1]
    qty = int(interval[:-1])
    per_year = {"m": 365 * 24 * 60, "h": 365 * 24, "d": 365}.get(unit)
    if per_year is None:
        return float(MINUTES_PER_YEAR)
    return per_year / qty
