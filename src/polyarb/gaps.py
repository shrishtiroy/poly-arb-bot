"""Gap detection.

All three arb shapes reduce to one test: a *partition* — a set of legs whose
outcomes are mutually exclusive and jointly exhaustive, so holding one share of
each pays exactly $1 at resolution. If the depth-weighted cost of buying every
leg is under $1, the difference is the gross edge.

    BINARY       YES + NO of one market on one venue
    MULTI        YES of every market in a neg-risk group on one venue
    CROSS_VENUE  complementary outcomes on different venues (manual mapping)

Depth honesty is the whole point: prices are walked through the book for the
target size, and the executable size is the *minimum* fillable across all legs.
Top-of-book "gaps" with no size behind them never make it out of here.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from .config import Config
from .fees import fee_model_for
from .models import (
    Category,
    GapEvent,
    GapKind,
    GapLeg,
    Market,
    OrderBook,
    Venue,
)

log = logging.getLogger(__name__)

BookKey = tuple[Venue, str, str]  # (venue, market_id, outcome_id)


@dataclass
class Partition:
    """A payout-complete set of legs; buying all legs pays exactly $1."""

    kind: GapKind
    key: str  # stable identity for open/close tracking
    category: Category
    legs: list[BookKey]
    # note for cross-venue: resolution criteria may differ subtly between
    # venues; the mapping file is trusted to only pair true equivalents.


def load_mappings(path: str | Path) -> list[Partition]:
    """mappings.yaml format:

    cross_venue:
      - name: "NBA Finals G7: Celtics win"
        category: sports
        legs:
          - {venue: polymarket, market_id: "0xabc", outcome_id: "123"}   # YES
          - {venue: novig,      market_id: "9",     outcome_id: "9-away"} # complement
    """
    path = Path(path)
    if not path.exists():
        return []
    data = yaml.safe_load(path.read_text()) or {}
    partitions = []
    for entry in data.get("cross_venue", []):
        legs = [
            (Venue(l["venue"]), str(l["market_id"]), str(l["outcome_id"]))
            for l in entry["legs"]
        ]
        partitions.append(
            Partition(
                kind=GapKind.CROSS_VENUE,
                key="xv:" + entry.get("name", "|".join(f"{v}:{o}" for v, _, o in legs)),
                category=Category(entry.get("category", "sports")),
                legs=legs,
            )
        )
    log.info("loaded %d cross-venue mappings from %s", len(partitions), path)
    return partitions


def yes_outcome_id(market: Market) -> str:
    for outcome in market.outcomes:
        if outcome.name.strip().lower() == "yes":
            return outcome.outcome_id
    return market.outcomes[0].outcome_id


def build_partitions(markets: list[Market], mappings: list[Partition]) -> list[Partition]:
    partitions: list[Partition] = list(mappings)
    groups: dict[tuple[Venue, str], list[Market]] = {}
    for market in markets:
        if market.is_binary:
            partitions.append(
                Partition(
                    kind=GapKind.BINARY,
                    key=f"bin:{market.venue.value}:{market.market_id}",
                    category=market.category,
                    legs=[
                        (market.venue, market.market_id, o.outcome_id)
                        for o in market.outcomes
                    ],
                )
            )
        if market.group_id:
            groups.setdefault((market.venue, market.group_id), []).append(market)
    for (venue, group_id), members in groups.items():
        if len(members) < 3:  # a 2-market group adds nothing over BINARY
            continue
        # Completeness check: a MULTI partition is only a valid arb if it holds
        # EVERY outcome of the group (mutually exclusive AND jointly exhaustive).
        # The venue reports the true group size; if we discovered fewer members
        # (the discovery cap sliced the group), buying our legs does NOT
        # guarantee $1 — the winner may be an outcome we don't hold. Skip it.
        expected = next((m.group_size for m in members
                         if m.group_size is not None), None)
        if expected is not None and len(members) < expected:
            log.warning(
                "skipping incomplete neg-risk group %s: discovered %d of %d "
                "outcomes (raise max_markets_per_venue to watch it)",
                group_id, len(members), expected,
            )
            continue
        partitions.append(
            Partition(
                kind=GapKind.MULTI,
                key=f"multi:{venue.value}:{group_id}",
                category=members[0].category,
                legs=[(m.venue, m.market_id, yes_outcome_id(m)) for m in members],
            )
        )
    return partitions


@dataclass
class OpenGap:
    gap_id: str
    ts_open: float


@dataclass
class GapDetector:
    config: Config
    markets: dict[tuple[Venue, str], Market]
    partitions: list[Partition]
    books: dict[BookKey, OrderBook] = field(default_factory=dict)
    open_gaps: dict[str, OpenGap] = field(default_factory=dict)  # partition key →
    _by_book: dict[BookKey, list[Partition]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for partition in self.partitions:
            for leg in partition.legs:
                self._by_book.setdefault(leg, []).append(partition)

    def on_book(self, book: OrderBook) -> tuple[list[GapEvent], list[tuple[str, str, float]]]:
        """Ingest a book snapshot. Returns (newly_opened, closed) where closed
        entries are (partition_key, gap_id, ts)."""
        key: BookKey = (book.venue, book.market_id, book.outcome_id)
        self.books[key] = book
        opened: list[GapEvent] = []
        closed: list[tuple[str, str, float]] = []
        for partition in self._by_book.get(key, []):
            gap = self._evaluate(partition, book.ts)
            is_open = partition.key in self.open_gaps
            if gap is not None and not is_open:
                self.open_gaps[partition.key] = OpenGap(gap.gap_id, gap.ts)
                opened.append(gap)
            elif gap is None and is_open:
                prior = self.open_gaps.pop(partition.key)
                closed.append((partition.key, prior.gap_id, book.ts))
        return opened, closed

    def _evaluate(self, partition: Partition, ts: float) -> GapEvent | None:
        target = self.config.target_notional  # $1-payout shares ≈ dollars
        legs: list[GapLeg] = []
        executable = target
        for venue, market_id, outcome_id in partition.legs:
            book = self.books.get((venue, market_id, outcome_id))
            if book is None or not book.asks:
                return None
            filled, _ = book.cost_to_buy(target)
            executable = min(executable, filled)
        if executable < self.config.min_executable_shares:
            return None
        total_cost = 0.0
        fees_per_share = 0.0
        for venue, market_id, outcome_id in partition.legs:
            book = self.books[(venue, market_id, outcome_id)]
            filled, cost = book.cost_to_buy(executable)
            if filled + 1e-9 < executable:
                return None
            avg_price = cost / executable
            legs.append(GapLeg(venue=venue, market_id=market_id,
                               outcome_id=outcome_id, avg_price=avg_price))
            total_cost += cost
            market = self.markets.get((venue, market_id))
            if market is not None:
                fee_model = fee_model_for(market)
                fees_per_share += fee_model.taker_fee(1.0, avg_price)
        gross = 1.0 - total_cost / executable
        if gross < self.config.min_gross_edge:
            return None
        # Completeness guard: an implausibly large edge on a multi-outcome group
        # means the partition is missing legs (the discovery cap sliced the
        # neg-risk group), so it is not jointly exhaustive and the "$1 payout"
        # guarantee is false. Drop it rather than book a phantom arb.
        if (partition.kind is GapKind.MULTI
                and gross > self.config.max_multi_gross_edge):
            log.warning(
                "dropping incomplete multi partition %s: gross edge %.3f > %.3f "
                "(only %d legs seen — likely sliced by max_markets_per_venue)",
                partition.key, gross, self.config.max_multi_gross_edge,
                len(partition.legs),
            )
            return None
        return GapEvent(
            kind=partition.kind,
            ts=ts,
            category=partition.category,
            legs=legs,
            executable_shares=executable,
            gross_edge_per_share=gross,
            taker_fees_per_share=fees_per_share,
            net_taker_edge_per_share=gross - fees_per_share,
        )
