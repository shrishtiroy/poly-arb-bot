"""Comparison report and deploy decision.

The report is the deliverable of the whole harness: one table comparing every
(venue × category × policy) cell on identical detected gaps, plus gap
frequency/lifetime stats, feeding `decide()` which emits an explicit
DEPLOY / NO-DEPLOY verdict per the criteria in Config.decision.
"""

from __future__ import annotations

import sqlite3
import statistics
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

from .config import Config
from .models import Venue

# Venue-specific live-deploy caveats surfaced with any DEPLOY verdict.
JURISDICTION_NOTES = {
    Venue.NOVIG.value: (
        "Novig operates under a sweepstakes model with state-by-state "
        "availability - confirm your state is eligible before funding."
    ),
    Venue.POLYMARKET.value: (
        "US users trade via Polymarket US; confirm your account type matches "
        "the fee schedule measured here."
    ),
    Venue.KALSHI.value: "CFTC-regulated; US-friendly.",
}


@dataclass
class Cell:
    venue: str
    category: str
    policy: str
    kind: str  # binary / multi / cross_venue - kept separate so a
               # venue-vs-venue strategy never inflates a single-venue row
    n_attempts: int = 0
    n_captured: int = 0
    n_leg_risk: int = 0
    n_unfilled: int = 0
    n_missed: int = 0  # TAKER_DISCIPLINED only: edge closed during simulated latency
    gross_pnl: float = 0.0
    fees: float = 0.0
    rebate_est: float = 0.0
    net_pnl: float = 0.0
    daily_pnl: dict[int, float] = None  # epoch-day → net pnl

    def __post_init__(self):
        if self.daily_pnl is None:
            self.daily_pnl = defaultdict(float)

    @property
    def net_plus_rebate(self) -> float:
        return self.net_pnl + self.rebate_est

    @property
    def fill_rate(self) -> float:
        return self.n_captured / self.n_attempts if self.n_attempts else 0.0

    @property
    def pnl_without_best_day(self) -> float:
        if not self.daily_pnl:
            return self.net_pnl
        return self.net_pnl - max(self.daily_pnl.values())

    @property
    def days_covered(self) -> float:
        if not self.daily_pnl:
            return 0.0
        return len(self.daily_pnl)


def load_cells(db_path: str | Path) -> tuple[list[Cell], dict]:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    cells: dict[tuple[str, str, str], Cell] = {}
    for row in conn.execute("SELECT * FROM results"):
        key = (row["venue"], row["category"], row["policy"], row["kind"])
        cell = cells.setdefault(key, Cell(*key))
        cell.n_attempts += 1
        if row["outcome"] == "captured":
            cell.n_captured += 1
        elif row["outcome"] == "leg_risk":
            cell.n_leg_risk += 1
        elif row["outcome"] == "unfilled":
            cell.n_unfilled += 1
        elif row["outcome"] == "missed":
            cell.n_missed += 1
        cell.gross_pnl += row["gross_pnl"]
        cell.fees += row["fees"]
        cell.rebate_est += row["rebate_est"]
        cell.net_pnl += row["net_pnl"]
        cell.daily_pnl[int(row["ts_close"] // 86400)] += row["net_pnl"]

    gap_stats: dict = {"by_kind": {}, "lifetimes": {}}
    for row in conn.execute(
        "SELECT kind, COUNT(*) n, AVG(gross_edge_per_share) avg_edge, "
        "AVG(net_taker_edge_per_share) avg_net_taker FROM gap_events GROUP BY kind"
    ):
        gap_stats["by_kind"][row["kind"]] = dict(row)
    lifetimes = defaultdict(list)
    for row in conn.execute(
        "SELECT g.kind kind, l.ts_close - l.ts_open dt FROM gap_lifetimes l "
        "JOIN gap_events g ON g.gap_id = l.gap_id"
    ):
        lifetimes[row["kind"]].append(row["dt"])
    for kind, values in lifetimes.items():
        values.sort()
        gap_stats["lifetimes"][kind] = {
            "p50": statistics.median(values),
            "p90": values[min(len(values) - 1, int(len(values) * 0.9))],
        }
    conn.close()
    return sorted(cells.values(), key=lambda c: -c.net_pnl), gap_stats


def render_report(db_path: str | Path) -> str:
    cells, gap_stats = load_cells(db_path)
    lines = []
    lines.append("=" * 100)
    lines.append("POLYARB - cross-venue paper-trading comparison")
    lines.append("=" * 100)
    lines.append("")
    lines.append("Gap population:")
    for kind, stats in gap_stats["by_kind"].items():
        life = gap_stats["lifetimes"].get(kind, {})
        lines.append(
            f"  {kind:<12} n={stats['n']:<6} "
            f"avg gross edge {stats['avg_edge']*100:5.2f}c/share   "
            f"avg net-taker edge {stats['avg_net_taker']*100:5.2f}c/share   "
            f"lifetime p50={life.get('p50', float('nan')):6.1f}s "
            f"p90={life.get('p90', float('nan')):6.1f}s"
        )
    lines.append("")
    header = (
        f"{'venue':<11}{'category':<10}{'strategy':<12}{'policy':<7}"
        f"{'tries':>6}{'capt':>6}"
        f"{'legrisk':>8}{'missed':>7}{'fill%':>7}{'gross$':>10}{'fees$':>9}{'net$':>10}"
        f"{'net+reb$':>10}{'days':>6}"
    )
    lines.append(header)
    lines.append("-" * len(header))
    for cell in cells:
        venue_label = "(multi)" if cell.kind == "cross_venue" else cell.venue
        lines.append(
            f"{venue_label:<11}{cell.category:<10}{cell.kind:<12}{cell.policy:<7}"
            f"{cell.n_attempts:>6}{cell.n_captured:>6}{cell.n_leg_risk:>8}"
            f"{cell.n_missed:>7}"
            f"{cell.fill_rate*100:>6.1f}%{cell.gross_pnl:>10.2f}{cell.fees:>9.2f}"
            f"{cell.net_pnl:>10.2f}{cell.net_plus_rebate:>10.2f}"
            f"{cell.days_covered:>6.0f}"
        )
    if not cells:
        lines.append("  (no attempts recorded - run `polyarb collect` first)")
    lines.append("")
    lines.append("Notes: taker rows assume perfectly simultaneous fills (optimistic).")
    lines.append("       taker_disc waits config.taker_disc_latency_s and refills against")
    lines.append("       the book as it then stands; 'missed' = edge closed in that window.")
    lines.append("       maker 'net+reb$' uses an UPPER-BOUND rebate estimate.")
    return "\n".join(lines)


def decide(db_path: str | Path, config: Config) -> str:
    cells, gap_stats = load_cells(db_path)
    criteria = config.decision
    lines = []
    deployables: list[tuple[Cell, str]] = []
    reasons: dict[str, str] = {}

    for cell in cells:
        why = None
        if cell.days_covered < criteria.min_days_of_data:
            why = (f"only {cell.days_covered:.0f} day(s) of data "
                   f"(need {criteria.min_days_of_data:.0f})")
        elif cell.n_captured < criteria.min_captures:
            why = f"only {cell.n_captured} captures (need {criteria.min_captures})"
        elif cell.net_pnl <= criteria.min_net_pnl:
            why = f"net P&L ${cell.net_pnl:.2f} not positive after fees"
        elif (criteria.require_positive_without_best_day
              and cell.pnl_without_best_day <= 0):
            why = (f"edge vanishes without best day "
                   f"(${cell.pnl_without_best_day:.2f}) - looks like a fluke")
        if why is None:
            note = (
                "cross-venue strategy - confirm jurisdiction on every leg venue"
                if cell.kind == "cross_venue"
                else JURISDICTION_NOTES.get(cell.venue, "")
            )
            deployables.append((cell, note))
        else:
            key = f"{cell.venue}/{cell.category}/{cell.kind}/{cell.policy}"
            reasons[key] = why

    lines.append("=" * 100)
    lines.append("POLYARB - deploy decision")
    lines.append("=" * 100)
    if deployables:
        best, note = deployables[0]
        lines.append("")
        lines.append(f"VERDICT: DEPLOY(venue={best.venue}, category={best.category}, "
                     f"strategy={best.kind}, policy={best.policy})")
        lines.append(f"  net P&L ${best.net_pnl:.2f} over {best.days_covered:.0f} days, "
                     f"{best.n_captured} captures, fill rate {best.fill_rate*100:.1f}%, "
                     f"without-best-day ${best.pnl_without_best_day:.2f}")
        if note:
            lines.append(f"  jurisdiction: {note}")
        for cell, note in deployables[1:]:
            lines.append(f"  also qualified: {cell.venue}/{cell.category}/"
                         f"{cell.kind}/{cell.policy} (net ${cell.net_pnl:.2f})")
    else:
        lines.append("")
        lines.append("VERDICT: NO-DEPLOY")
        lines.append("  No (venue x category x policy) cell met every criterion:")
        for key, why in list(reasons.items())[:12]:
            lines.append(f"    {key:<40} {why}")
        if not cells:
            lines.append("    (no data at all - run `polyarb collect` first)")
    lines.append("")
    lines.append("Criteria: "
                 f">={criteria.min_days_of_data:.0f} days, "
                 f">={criteria.min_captures} captures, net P&L > "
                 f"${criteria.min_net_pnl:.2f}, robust to removing best day: "
                 f"{criteria.require_positive_without_best_day}")
    return "\n".join(lines)
