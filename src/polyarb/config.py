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
    # multi gap whose gross edge exceeds this — it is a phantom, not money.
    max_multi_gross_edge: float = 0.15

    # Paper trading
    bankroll_per_venue: float = 10_000.0
    maker_order_ttl_s: float = 120.0     # cancel resting legs after this
    maker_price_improve_tick: float = 0.01

    # Storage
    db_path: str = "polyarb.sqlite"
    event_mappings_path: str = "mappings.yaml"

    # Live collection
    kalshi_poll_interval_s: float = 1.0
    novig_poll_interval_s: float = 1.0
    max_markets_per_venue: int = 200

    decision: DecisionCriteria = DecisionCriteria()

    @classmethod
    def load(cls, path: str | Path | None) -> "Config":
        if path is None:
            return cls()
        data = yaml.safe_load(Path(path).read_text()) or {}
        return cls.model_validate(data)
