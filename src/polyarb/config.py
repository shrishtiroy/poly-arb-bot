"""Run configuration. Defaults are deliberately conservative; override via a
YAML file passed to the CLI (`polyarb collect --config my.yaml`)."""

from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel

from .models import Venue


class DecisionCriteria(BaseModel):
    # A (venue, category, policy) cell must clear ALL of these to earn DEPLOY.
    min_days_of_data: float = 7.0
    min_captures: int = 30
    min_net_pnl: float = 0.0
    # Still profitable after deleting the single best day (fluke filter).
    require_positive_without_best_day: bool = True


class Config(BaseModel):
    venues: list[Venue] = [Venue.POLYMARKET, Venue.KALSHI, Venue.NOVIG]

    # Gap detection
    target_notional: float = 500.0       # dollars we pretend to deploy per gap
    min_gross_edge: float = 0.005        # 0.5 cents/share before we even look
    min_executable_shares: float = 100.0  # below this a "gap" is thin-book noise
    # A real arb edge is a cent or two. A huge "edge" on a MULTI group is the
    # signature of an INCOMPLETE partition: the discovery cap
    # (max_markets_per_venue) sliced a neg-risk group, so the legs we hold are
    # not jointly exhaustive and buying them does NOT guarantee $1. Reject any
    # multi gap whose gross edge exceeds this - it is a phantom, not money.
    max_multi_gross_edge: float = 0.15

    # Paper trading
    bankroll_per_venue: float = 10_000.0
    maker_order_ttl_s: float = 120.0     # cancel resting legs after this
    maker_price_improve_tick: float = 0.01
    # Maker is OFF by default: across 1,296 live attempts it completed zero
    # two-leg arbs (1,271 expired unfilled, 25 filled one leg and were
    # liquidated as leg_risk for -$477). It is pure downside on this data, so
    # the harness no longer rests orders. Set true to re-measure it.
    enable_maker: bool = False

    # Storage
    db_path: str = "polyarb.sqlite"
    event_mappings_path: str = "mappings.yaml"

    # Live collection
    kalshi_poll_interval_s: float = 1.0
    novig_poll_interval_s: float = 1.0
    max_markets_per_venue: int = 200
    # Per-venue overrides for the discovery cap. Missing venues fall back to
    # max_markets_per_venue. Keys are Venue values, e.g. {"polymarket": 300}.
    max_markets_by_venue: dict[str, int] = {}

    # Ladder arb (Strategy A) needs a DIVERSE set of threshold-market events, not
    # just whatever survives the volume-ordered cap. After the main discovery
    # pass, page further specifically for threshold ("above/below/hit $X")
    # markets until rungs span at least this many distinct events, so ladders are
    # not dominated by a single asset. Bounded by ladder_scan_max_pages.
    ladder_min_events: int = 20
    ladder_scan_max_pages: int = 40

    # Strategy B: Black-Scholes binary fair-value measurement (crypto threshold
    # markets valued as digital options). All knobs live here and are recorded
    # per measurement, so model performance is tunable data-driven, not guessed.
    strategy_b_enabled: bool = True
    binance_base: str = "https://api.binance.us"
    bs_risk_free_rate: float = 0.0        # r in d2; ~0 for short-dated crypto
    vol_lookback_min: int = 1440          # klines of history for realized vol
    vol_kline_interval: str = "1m"        # kline granularity
    bs_price_mode: str = "mid"            # implied prob source: mid | ask
    strategy_b_min_divergence: float = 0.03  # record when |market - fair| >= this
    bs_refresh_spot_s: float = 5.0        # spot cache refresh cadence
    bs_refresh_vol_s: float = 300.0       # realized-vol cache refresh cadence
    bs_record_throttle_s: float = 30.0    # min seconds between rows per market
    bs_params_version: str = "b1"         # bump when the model/knobs change

    decision: DecisionCriteria = DecisionCriteria()

    def cap_for(self, venue: Venue) -> int:
        """Discovery cap for a venue: its override, else the global default."""
        return self.max_markets_by_venue.get(venue.value, self.max_markets_per_venue)

    @classmethod
    def load(cls, path: str | Path | None) -> "Config":
        if path is None:
            return cls()
        data = yaml.safe_load(Path(path).read_text()) or {}
        return cls.model_validate(data)
