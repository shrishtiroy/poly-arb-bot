"""Tests for Strategy A: ladder (monotonicity) arbitrage."""

from polyarb.ladders import build_ladder_partitions, parse_threshold
from polyarb.models import GapKind, Market, Outcome, Venue


def _mkt(question: str, event_slug: str, mid: str) -> Market:
    return Market(
        venue=Venue.POLYMARKET, market_id=mid, question=question,
        outcomes=[Outcome(outcome_id=f"{mid}-y", name="Yes"),
                  Outcome(outcome_id=f"{mid}-n", name="No")],
        event_slug=event_slug,
    )


def test_parse_threshold_families():
    assert parse_threshold("Will the price of Bitcoin be above $60,000 on July 16?") == ("up", 60000)
    assert parse_threshold("Will Bitcoin reach $67,500 in July?") == ("up", 67500)
    assert parse_threshold("Will Bitcoin dip to $55,000 in July?") == ("down", 55000)
    assert parse_threshold("Will Ethereum be below $1,100 in July?") == ("down", 1100)
    assert parse_threshold("Will BTC hit $2k?") == ("up", 2000)  # trailing k


def test_parse_threshold_rejects_non_threshold():
    assert parse_threshold("Will the Fed cut rates in July?") is None
    assert parse_threshold("Who wins the 2026 election?") is None


def test_parse_threshold_low_high_markers_override_verb():
    # "(LOW)/(HIGH)" markers state direction outright and must override the
    # ambiguous verb "hit" (which alone reads as "up"). Otherwise a floor and a
    # ceiling question would form a phantom ladder.
    assert parse_threshold("Will WTI Crude Oil (WTI) hit (LOW) $65 in July?") == ("down", 65)
    assert parse_threshold("Will WTI Crude Oil (WTI) hit (HIGH) $85 in July?") == ("up", 85)
    assert parse_threshold("Will WTI Crude Oil (WTI) hit (LOW) $60 in July?") == ("down", 60)


def test_low_high_questions_do_not_share_a_ladder():
    # A (LOW) floor question and a (HIGH) ceiling question of the same event must
    # NOT pair: they are opposite families, so no bogus ladder partition forms.
    markets = [
        _mkt("Will WTI Crude Oil (WTI) hit (LOW) $65 in July?", "wti-jul", "a"),
        _mkt("Will WTI Crude Oil (WTI) hit (HIGH) $85 in July?", "wti-jul", "b"),
    ]
    assert build_ladder_partitions(markets) == []


def test_up_ladder_builds_all_pairs():
    markets = [
        _mkt("Bitcoin above $60,000 on July 16?", "btc-jul16", "a"),
        _mkt("Bitcoin above $64,000 on July 16?", "btc-jul16", "b"),
        _mkt("Bitcoin above $68,000 on July 16?", "btc-jul16", "c"),
    ]
    parts = build_ladder_partitions(markets)
    # Every rung pair, not just adjacent: (60,64), (60,68), (64,68).
    assert len(parts) == 3
    assert all(p.kind is GapKind.LADDER for p in parts)
    # up family: easier = lower strike -> its YES leg is first; harder -> NO leg.
    first = parts[0]
    assert first.legs[0] == (Venue.POLYMARKET, "a", "a-y")  # YES of the $60k rung
    assert first.legs[1] == (Venue.POLYMARKET, "b", "b-n")  # NO of the $64k rung
    # The non-adjacent $60k vs $68k pair is present too.
    assert any(p.legs == [(Venue.POLYMARKET, "a", "a-y"),
                          (Venue.POLYMARKET, "c", "c-n")] for p in parts)


def test_ladder_pair_count_is_n_choose_2():
    # 4 rungs -> C(4,2) = 6 partitions.
    markets = [
        _mkt("Bitcoin above $60,000 on July 16?", "btc-jul16", "a"),
        _mkt("Bitcoin above $62,000 on July 16?", "btc-jul16", "b"),
        _mkt("Bitcoin above $64,000 on July 16?", "btc-jul16", "c"),
        _mkt("Bitcoin above $66,000 on July 16?", "btc-jul16", "d"),
    ]
    assert len(build_ladder_partitions(markets)) == 6


def test_down_ladder_flips_easier_side():
    markets = [
        _mkt("Bitcoin dip to $50,000 in July?", "btc-jul", "a"),
        _mkt("Bitcoin dip to $55,000 in July?", "btc-jul", "b"),
    ]
    parts = build_ladder_partitions(markets)
    assert len(parts) == 1
    # down family: higher strike ($55k) is easier -> its YES leg first.
    assert parts[0].legs[0] == (Venue.POLYMARKET, "b", "b-y")
    assert parts[0].legs[1] == (Venue.POLYMARKET, "a", "a-n")


def test_different_events_not_mixed():
    markets = [
        _mkt("Bitcoin above $60,000 on July 16?", "btc-jul16", "a"),
        _mkt("Bitcoin above $64,000 on July 20?", "btc-jul20", "b"),
    ]
    assert build_ladder_partitions(markets) == []
