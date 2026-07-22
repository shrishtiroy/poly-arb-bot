"""Strategy A: cross-market monotonicity arbitrage ("ladder arb").

Polymarket lists ladders of threshold markets on the same asset and the same
resolution date, e.g. "BTC above $60k / $62k / $68k on Jul 16". The YES
probability must be monotonic in the strike:

    "above X" / "reach X":  P falls as strike rises   (higher bar = harder)
    "dip to X" / "below X": P rises as strike rises    (lower bar = harder)

So for any two rungs there is an "easier" one that is strictly more likely to
pay. Buying YES of the easier rung plus NO of the harder rung is a two-leg
basket that pays >= $1 in EVERY outcome. If that basket costs under $1, the
difference is locked, risk-free profit, which is exactly the arb test the gap
detector already runs. This module only has to build the right partitions; the
detector, fees, and paper engine handle the rest.
"""

from __future__ import annotations

import re

from .gaps import Partition, no_outcome_id, yes_outcome_id
from .models import GapKind, Market

# Some events tag each rung with an explicit direction marker, e.g.
# "Will WTI Crude Oil hit (LOW) $65 in July?" vs "... hit (HIGH) $85 ...".
# These state the direction outright and MUST override the ambiguous verb
# "hit" (which otherwise reads as "up"), else a floor question and a ceiling
# question get lumped into the same ladder and the monotonicity guarantee,
# and any "arb" built on it, is bogus.
_LOW_MARK = re.compile(r"\(\s*low\s*\)", re.I)
_HIGH_MARK = re.compile(r"\(\s*high\s*\)", re.I)
# "up" family: probability falls as the strike rises. "down": it rises.
_UP = re.compile(r"\b(above|reach|hit|over|exceed)\b", re.I)
_DOWN = re.compile(r"\b(dip|below|under|drop|fall)\b", re.I)
_STRIKE = re.compile(r"\$\s*([\d,]+(?:\.\d+)?)\s*(k)?", re.I)


def parse_threshold(question: str) -> tuple[str, float] | None:
    """Return (family, strike) for a threshold question, else None.

    family is "up" (P falls as strike rises) or "down" (P rises as strike rises).
    Explicit (LOW)/(HIGH) markers take priority over verb keywords.
    """
    money = _STRIKE.search(question)
    if not money:
        return None
    strike = float(money.group(1).replace(",", ""))
    if money.group(2):  # a trailing "k", e.g. "$67.5k"
        strike *= 1000
    # Explicit direction markers win over ambiguous verbs.
    if _LOW_MARK.search(question):
        return "down", strike
    if _HIGH_MARK.search(question):
        return "up", strike
    if _DOWN.search(question):
        return "down", strike
    if _UP.search(question):
        return "up", strike
    return None


def build_ladder_partitions(markets: list[Market]) -> list[Partition]:
    """Group same-event, same-family threshold markets and emit a 2-leg
    partition for EVERY rung pair (legs pay >= $1 always).

    Every pair of rungs in a monotonic ladder is a valid arb, not just adjacent
    ones: for any two strikes the higher-probability rung must cost at least as
    much as the lower, so YES(easier) + NO(harder) always pays >= $1. Wider gaps
    (e.g. $58k vs $68k) are more likely to show a monotonicity break than
    neighbors, so all combinations are checked, not just consecutive rungs.
    """
    from itertools import combinations

    groups: dict[tuple, list[tuple[float, Market]]] = {}
    for m in markets:
        if not m.is_binary or not m.event_slug:
            continue
        parsed = parse_threshold(m.question)
        if parsed is None:
            continue
        family, strike = parsed
        groups.setdefault((m.venue, m.event_slug, family), []).append((strike, m))

    partitions: list[Partition] = []
    for (venue, event_slug, family), rungs in groups.items():
        if len(rungs) < 2:
            continue
        rungs.sort(key=lambda r: r[0])  # by strike, ascending
        for (lo_strike, lo), (hi_strike, hi) in combinations(rungs, 2):
            if lo_strike == hi_strike:
                continue  # two markets at the same strike are not a ladder
            # "easier" rung: the one strictly more likely to resolve YES.
            # up family: lower strike is easier. down family: higher strike.
            easier, harder = (lo, hi) if family == "up" else (hi, lo)
            partitions.append(
                Partition(
                    kind=GapKind.LADDER,
                    key=f"ladder:{venue.value}:{event_slug}:{family}:{lo_strike}-{hi_strike}",
                    category=easier.category,
                    legs=[
                        (easier.venue, easier.market_id, yes_outcome_id(easier)),
                        (harder.venue, harder.market_id, no_outcome_id(harder)),
                    ],
                )
            )
    return partitions
