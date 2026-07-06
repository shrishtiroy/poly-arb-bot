"""Adapter interface every venue implements.

Adapters translate venue-native payloads into the normalized models in
polyarb.models. Everything downstream (gap detection, paper trading,
reporting) is venue-agnostic and consumes only FeedEvents.

Read-only by design: the interface has no order-placement method, so phase 0
cannot trade live even by misconfiguration.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator

from ..config import Config
from ..models import FeedEvent, Market, Venue


class VenueAdapter(ABC):
    venue: Venue

    def __init__(self, config: Config):
        self.config = config

    @abstractmethod
    async def discover_markets(self) -> list[Market]:
        """Fetch active markets (capped at config.max_markets_per_venue)."""

    @abstractmethod
    def stream_events(self, markets: list[Market]) -> AsyncIterator[FeedEvent]:
        """Yield normalized book snapshots and trades for the given markets."""
