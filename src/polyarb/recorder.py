"""SQLite persistence for gaps, fills, and attempt results."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from .models import GapEvent, GapResult, Market, PaperFill

SCHEMA = """
CREATE TABLE IF NOT EXISTS gap_events (
    gap_id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    ts REAL NOT NULL,
    category TEXT NOT NULL,
    executable_shares REAL NOT NULL,
    gross_edge_per_share REAL NOT NULL,
    taker_fees_per_share REAL NOT NULL,
    net_taker_edge_per_share REAL NOT NULL,
    legs TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS gap_lifetimes (
    gap_id TEXT PRIMARY KEY,
    partition_key TEXT NOT NULL,
    ts_open REAL NOT NULL,
    ts_close REAL NOT NULL
);
-- Human-readable reference for the opaque market_id/outcome_id in gaps & fills,
-- so a dashboard can show "Will France win?" / "Yes" instead of hex token ids.
CREATE TABLE IF NOT EXISTS markets (
    venue TEXT NOT NULL,
    market_id TEXT NOT NULL,
    question TEXT NOT NULL,
    category TEXT NOT NULL,
    outcomes TEXT NOT NULL,  -- json: {outcome_id: name}
    slug TEXT,               -- link slug (polymarket market / kalshi ticker)
    event_slug TEXT,         -- polymarket event slug (grouped markets)
    end_ts REAL,             -- market expiry (unix seconds), when exposed
    PRIMARY KEY (venue, market_id)
);
CREATE TABLE IF NOT EXISTS results (
    gap_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    policy TEXT NOT NULL,
    venue TEXT NOT NULL,
    category TEXT NOT NULL,
    outcome TEXT NOT NULL,
    shares REAL NOT NULL,
    gross_pnl REAL NOT NULL,
    fees REAL NOT NULL,
    rebate_est REAL NOT NULL,
    net_pnl REAL NOT NULL,
    ts_open REAL NOT NULL,
    ts_close REAL NOT NULL,
    detail TEXT,  -- json: policy-specific extras (maker resting limit prices)
    PRIMARY KEY (gap_id, policy)
);
CREATE TABLE IF NOT EXISTS fills (
    order_id TEXT NOT NULL,
    gap_id TEXT NOT NULL,
    policy TEXT NOT NULL,
    venue TEXT NOT NULL,
    market_id TEXT NOT NULL,
    outcome_id TEXT NOT NULL,
    category TEXT NOT NULL,
    price REAL NOT NULL,
    size REAL NOT NULL,
    fee REAL NOT NULL,
    rebate_est REAL NOT NULL,
    ts REAL NOT NULL,
    note TEXT NOT NULL
);
-- Strategy B: Black-Scholes binary fair-value measurements. Each row is one
-- valuation of a crypto threshold market against an options model, PLUS the
-- hyperparameters used to produce it (so runs are comparable and tunable).
-- Additive: measurement-only, no bearing on the arb accounting above.
CREATE TABLE IF NOT EXISTS bs_measurements (
    ts REAL NOT NULL,
    market_id TEXT NOT NULL,
    outcome_id TEXT NOT NULL,
    question TEXT NOT NULL,
    asset TEXT NOT NULL,
    symbol TEXT NOT NULL,
    family TEXT NOT NULL,
    strike REAL NOT NULL,
    spot REAL NOT NULL,
    sigma REAL NOT NULL,
    t_years REAL NOT NULL,
    fair_prob REAL NOT NULL,
    market_prob REAL NOT NULL,
    divergence REAL NOT NULL,   -- market_prob - fair_prob
    r REAL NOT NULL,
    vol_lookback_min INTEGER NOT NULL,
    kline_interval TEXT NOT NULL,
    periods_per_year REAL NOT NULL,
    price_mode TEXT NOT NULL,
    params_version TEXT NOT NULL
);
"""


class Recorder:
    def __init__(self, db_path: str | Path):
        self.conn = sqlite3.connect(str(db_path))
        # SQLite allows one writer at a time and, by default, waits only a few
        # seconds for a contended lock before raising "database is locked" - an
        # unhandled crash that had killed the whole collector when a write
        # (e.g. a Strategy B measurement) coincided with the dashboard reading.
        # WAL lets readers and the writer proceed concurrently; busy_timeout
        # makes any remaining contention wait instead of throwing.
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA busy_timeout=30000")  # 30s
        self.conn.executescript(SCHEMA)
        self._migrate()

    def _migrate(self) -> None:
        """Add columns introduced after a DB was first created. CREATE TABLE IF
        NOT EXISTS will not alter an existing table, so bring older DBs forward
        idempotently (OperationalError = column already present)."""
        for column in ("slug TEXT", "event_slug TEXT", "end_ts REAL"):
            try:
                self.conn.execute(f"ALTER TABLE markets ADD COLUMN {column}")
            except sqlite3.OperationalError:
                pass
        try:
            self.conn.execute("ALTER TABLE results ADD COLUMN detail TEXT")
        except sqlite3.OperationalError:
            pass

    def record_markets(self, markets: list[Market]) -> None:
        """Persist market metadata so gaps/fills can be shown human-readably."""
        for m in markets:
            self.conn.execute(
                "INSERT OR REPLACE INTO markets "
                "(venue, market_id, question, category, outcomes, slug, "
                "event_slug, end_ts) VALUES (?,?,?,?,?,?,?,?)",
                (m.venue.value, m.market_id, m.question, m.category.value,
                 json.dumps({o.outcome_id: o.name for o in m.outcomes}),
                 m.slug, m.event_slug, m.end_ts),
            )

    def record_gap(self, gap: GapEvent) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO gap_events VALUES (?,?,?,?,?,?,?,?,?)",
            (
                gap.gap_id, gap.kind.value, gap.ts, gap.category.value,
                gap.executable_shares, gap.gross_edge_per_share,
                gap.taker_fees_per_share, gap.net_taker_edge_per_share,
                json.dumps([leg.model_dump(mode="json") for leg in gap.legs]),
            ),
        )

    def record_gap_close(self, partition_key: str, gap_id: str,
                         ts_open: float, ts_close: float) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO gap_lifetimes VALUES (?,?,?,?)",
            (gap_id, partition_key, ts_open, ts_close),
        )

    def record_result(self, result: GapResult) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO results VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                result.gap_id, result.kind.value, result.policy.value,
                result.venue.value, result.category.value, result.outcome.value,
                result.shares, result.gross_pnl, result.fees, result.rebate_est,
                result.net_pnl, result.ts_open, result.ts_close,
                json.dumps(result.detail) if result.detail else None,
            ),
        )

    def record_fill(self, fill: PaperFill) -> None:
        self.conn.execute(
            "INSERT INTO fills VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                fill.order_id, fill.gap_id, fill.policy.value, fill.venue.value,
                fill.market_id, fill.outcome_id, fill.category.value,
                fill.price, fill.size, fill.fee, fill.rebate_est, fill.ts,
                fill.note,
            ),
        )

    def record_b_measurement(self, v) -> None:
        """Persist one Strategy B fair-value measurement (BinaryValuation)."""
        self.conn.execute(
            "INSERT INTO bs_measurements VALUES "
            "(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                v.ts, v.market_id, v.outcome_id, v.question, v.asset, v.symbol,
                v.family, v.strike, v.spot, v.sigma, v.t_years, v.fair_prob,
                v.market_prob, v.divergence, v.r, v.vol_lookback_min,
                v.kline_interval, v.periods_per_year, v.price_mode,
                v.params_version,
            ),
        )

    def commit(self) -> None:
        self.conn.commit()

    def close(self) -> None:
        self.conn.commit()
        self.conn.close()
