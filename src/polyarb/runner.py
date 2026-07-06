"""Event-loop core: wires detector → paper engine → recorder.

Consumes normalized FeedEvents from either live adapters or a replay file;
everything below the adapters is identical in both modes, which is what makes
the replay-mode tests meaningful.
"""

from __future__ import annotations

import logging
from pathlib import Path

from pydantic import TypeAdapter

from .config import Config
from .gaps import GapDetector, Partition, build_partitions, load_mappings
from .models import (
    BookEvent,
    FeedEvent,
    Market,
    MarketEvent,
    TradeEvent,
    Venue,
)
from .paper import PaperEngine
from .recorder import Recorder

log = logging.getLogger(__name__)

_EVENT_ADAPTER: TypeAdapter[FeedEvent] = TypeAdapter(FeedEvent)


def parse_event(line: str) -> FeedEvent:
    return _EVENT_ADAPTER.validate_json(line)


class Runner:
    def __init__(self, config: Config, markets: list[Market],
                 recorder: Recorder, mappings: list[Partition] | None = None):
        self.config = config
        self.recorder = recorder
        market_index = {(m.venue, m.market_id): m for m in markets}
        partitions = build_partitions(markets, mappings or [])
        self.detector = GapDetector(config=config, markets=market_index,
                                    partitions=partitions)
        self.engine = PaperEngine(config, market_index, self.detector.books)
        self._gap_open_ts: dict[str, float] = {}
        self.last_ts: float = 0.0

    def process(self, event: FeedEvent) -> None:
        self.last_ts = max(self.last_ts, event.ts)
        if isinstance(event, BookEvent):
            opened, closed = self.detector.on_book(event.book)
            self.engine.on_book(event.book, event.ts)
            for partition_key, gap_id, ts_close in closed:
                self.engine.on_gap_closed(gap_id, ts_close)
                self.recorder.record_gap_close(
                    partition_key, gap_id,
                    self._gap_open_ts.pop(gap_id, ts_close), ts_close,
                )
            for gap in opened:
                self._gap_open_ts[gap.gap_id] = gap.ts
                self.recorder.record_gap(gap)
                self.engine.on_gap(gap)
        elif isinstance(event, TradeEvent):
            self.engine.on_trade(event)
        elif isinstance(event, MarketEvent):
            # Live discovery happens upfront; replay loaders also pre-split
            # market events. Reaching here means an unsorted replay file.
            log.warning("market event mid-stream ignored: %s",
                        event.market.market_id)
        self._drain()

    def finalize(self) -> None:
        self.engine.finalize(self.last_ts)
        # Any gap still open at shutdown gets a lifetime record too.
        for partition_key, open_gap in self.detector.open_gaps.items():
            self.recorder.record_gap_close(
                partition_key, open_gap.gap_id, open_gap.ts_open, self.last_ts
            )
        self._drain()
        self.recorder.commit()

    def _drain(self) -> None:
        for result in self.engine.results:
            self.recorder.record_result(result)
        self.engine.results.clear()
        for fill in self.engine.fills:
            self.recorder.record_fill(fill)
        self.engine.fills.clear()


def load_replay(path: str | Path) -> tuple[list[Market], list[FeedEvent]]:
    """Read a JSONL replay file; market events seed discovery, the rest stream
    in timestamp order."""
    markets: list[Market] = []
    events: list[FeedEvent] = []
    with open(path) as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            event = parse_event(line)
            if isinstance(event, MarketEvent):
                markets.append(event.market)
            else:
                events.append(event)
    events.sort(key=lambda e: e.ts)
    return markets, events


def run_replay(config: Config, replay_dir: str | Path, recorder: Recorder) -> Runner:
    replay_path = Path(replay_dir)
    files = (sorted(replay_path.glob("*.jsonl"))
             if replay_path.is_dir() else [replay_path])
    if not files:
        raise FileNotFoundError(f"no .jsonl replay files under {replay_dir}")
    all_markets: list[Market] = []
    all_events: list[FeedEvent] = []
    for file in files:
        markets, events = load_replay(file)
        all_markets.extend(markets)
        all_events.extend(events)
    all_events.sort(key=lambda e: e.ts)
    mappings = load_mappings(config.event_mappings_path)
    runner = Runner(config, all_markets, recorder, mappings)
    for event in all_events:
        runner.process(event)
    runner.finalize()
    return runner


async def run_live(config: Config, hours: float, recorder: Recorder) -> Runner:
    import asyncio
    import time

    from .venues import KalshiAdapter, NovigAdapter, PolymarketAdapter

    adapter_types = {
        Venue.POLYMARKET: PolymarketAdapter,
        Venue.KALSHI: KalshiAdapter,
        Venue.NOVIG: NovigAdapter,
    }
    adapters = [adapter_types[v](config) for v in config.venues]

    markets: list[Market] = []
    live_adapters = []
    for adapter in adapters:
        try:
            discovered = await adapter.discover_markets()
            markets.extend(discovered)
            live_adapters.append((adapter, discovered))
        except Exception as exc:  # keep collecting from the venues that work
            log.error("%s discovery failed, skipping venue: %s",
                      adapter.venue.value, exc)

    if not markets:
        raise RuntimeError(
            "no venue reachable — if you are inside a sandboxed/proxied "
            "environment, run live collection from a machine with direct "
            "internet access (see README), or use --replay."
        )

    mappings = load_mappings(config.event_mappings_path)
    runner = Runner(config, markets, recorder, mappings)
    queue: asyncio.Queue[FeedEvent] = asyncio.Queue(maxsize=10_000)

    async def pump(adapter, adapter_markets):
        async for event in adapter.stream_events(adapter_markets):
            await queue.put(event)

    tasks = [asyncio.create_task(pump(a, m)) for a, m in live_adapters]
    deadline = time.time() + hours * 3600
    last_commit = time.time()
    try:
        while time.time() < deadline:
            try:
                event = await asyncio.wait_for(queue.get(), timeout=5.0)
            except asyncio.TimeoutError:
                continue
            runner.process(event)
            if time.time() - last_commit > 10:
                recorder.commit()
                last_commit = time.time()
    finally:
        for task in tasks:
            task.cancel()
        runner.finalize()
    return runner
