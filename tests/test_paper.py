"""Paper engine: taker math, maker FIFO queue, leg risk, accounting."""

import pytest

from conftest import make_book, make_market
from polyarb.gaps import GapDetector, build_partitions
from polyarb.models import (
    GapOutcomeKind,
    Policy,
    TradeEvent,
    Venue,
)
from polyarb.paper import PaperEngine


def build(config, category=None):
    market = make_market()
    if category:
        market.category = category
    markets = {(market.venue, market.market_id): market}
    det = GapDetector(config=config, markets=markets,
                      partitions=build_partitions([market], []))
    engine = PaperEngine(config, markets, det.books)
    return market, det, engine


def open_gap(det, engine, ts=1000.0):
    yes = make_book(Venue.POLYMARKET, "m1", "yes1",
                    [(0.46, 500)], [(0.48, 400)], ts)
    det.on_book(yes)
    engine.on_book(yes, ts)
    no = make_book(Venue.POLYMARKET, "m1", "no1",
                   [(0.47, 500)], [(0.49, 400)], ts)
    opened, closed = det.on_book(no)
    engine.on_book(no, ts)
    assert len(opened) == 1
    for gap in opened:
        engine.on_gap(gap)
    return opened[0]


def test_taker_capture_books_immediately(config):
    _, det, engine = build(config)
    gap = open_gap(det, engine)
    taker = [r for r in engine.results if r.policy == Policy.TAKER]
    assert len(taker) == 1
    result = taker[0]
    assert result.outcome == GapOutcomeKind.CAPTURED
    assert result.shares == pytest.approx(400)
    assert result.gross_pnl == pytest.approx(400 * 0.03)
    expected_fees = 400 * 0.03 * (0.48 * 0.52 + 0.49 * 0.51)
    assert result.fees == pytest.approx(expected_fees)
    assert result.net_pnl == pytest.approx(400 * 0.03 - expected_fees)


def test_taker_disciplined_waits_out_latency_then_reprices(config):
    _, det, engine = build(config)
    gap = open_gap(det, engine)  # ts=1000, exec queued for ts=1000+latency
    assert len(engine.pending_taker_disc) == 1
    assert not any(r.policy == Policy.TAKER_DISCIPLINED for r in engine.results)

    # A later tick past the latency window, book unchanged: fills at the same
    # prices detection saw, so it should match the TAKER row exactly.
    tick = make_book(Venue.POLYMARKET, "m1", "yes1",
                     [(0.46, 500)], [(0.48, 400)], ts=1001.0)
    det.on_book(tick)
    engine.on_book(tick, 1001.0)

    taker = [r for r in engine.results if r.policy == Policy.TAKER][0]
    disc = [r for r in engine.results if r.policy == Policy.TAKER_DISCIPLINED]
    assert len(disc) == 1
    assert disc[0].outcome == GapOutcomeKind.CAPTURED
    assert disc[0].net_pnl == pytest.approx(taker.net_pnl)
    assert not engine.pending_taker_disc


def test_taker_disciplined_misses_when_price_moves_during_latency(config):
    _, det, engine = build(config)
    gap = open_gap(det, engine)  # ts=1000
    assert len(engine.pending_taker_disc) == 1

    # Price moves against us inside the latency window - this update lands
    # before ts_exec so the sweep does not fire on it yet.
    worse = make_book(Venue.POLYMARKET, "m1", "yes1",
                      [(0.46, 500)], [(0.60, 400)], ts=1000.1)
    det.on_book(worse)
    engine.on_book(worse, 1000.1)
    assert not any(r.policy == Policy.TAKER_DISCIPLINED for r in engine.results)

    # Next tick lands past the latency window and executes against the book
    # as it now stands (still bad) rather than the stale detection price.
    tick = make_book(Venue.POLYMARKET, "m1", "yes1",
                     [(0.46, 500)], [(0.60, 400)], ts=1001.0)
    det.on_book(tick)
    engine.on_book(tick, 1001.0)

    disc = [r for r in engine.results if r.policy == Policy.TAKER_DISCIPLINED]
    assert len(disc) == 1
    assert disc[0].outcome == GapOutcomeKind.MISSED
    assert disc[0].net_pnl == 0.0
    assert not engine.pending_taker_disc


def test_maker_rests_and_fills_via_prints(config):
    _, det, engine = build(config)
    gap = open_gap(det, engine)
    attempt = engine.maker_attempts[gap.gap_id]
    yes_order = attempt.orders[(Venue.POLYMARKET, "m1", "yes1")]
    no_order = attempt.orders[(Venue.POLYMARKET, "m1", "no1")]
    # bid+tick: 0.47 and 0.48; improving prices → fresh level, no queue ahead
    assert yes_order.price == pytest.approx(0.47)
    assert no_order.price == pytest.approx(0.48)
    assert yes_order.queue_ahead == 0.0

    engine.on_trade(TradeEvent(ts=1010, venue=Venue.POLYMARKET, market_id="m1",
                               outcome_id="yes1", price=0.47, size=400,
                               side="sell"))
    assert yes_order.filled == pytest.approx(400)
    assert gap.gap_id in engine.maker_attempts  # one leg still open

    engine.on_trade(TradeEvent(ts=1020, venue=Venue.POLYMARKET, market_id="m1",
                               outcome_id="no1", price=0.48, size=400,
                               side="sell"))
    maker = [r for r in engine.results if r.policy == Policy.MAKER]
    assert len(maker) == 1
    result = maker[0]
    assert result.outcome == GapOutcomeKind.CAPTURED
    # locked: 400 * (1 - 0.47 - 0.48), zero maker fees on Polymarket
    assert result.net_pnl == pytest.approx(400 * 0.05)
    assert result.rebate_est > 0  # upper-bound rebate reported separately


def test_maker_fifo_queue_blocks_fill_until_prints_clear(config):
    _, det, engine = build(config)
    # Pre-existing size at the price we will join: bids already at 0.47
    yes = make_book(Venue.POLYMARKET, "m1", "yes1",
                    [(0.47, 250), (0.46, 500)], [(0.48, 400)], 1000)
    det.on_book(yes)
    no = make_book(Venue.POLYMARKET, "m1", "no1",
                   [(0.47, 500)], [(0.49, 400)], 1000)
    opened, _ = det.on_book(no)
    for gap in opened:
        engine.on_gap(gap)
    gap = opened[0]
    attempt = engine.maker_attempts[gap.gap_id]
    yes_order = attempt.orders[(Venue.POLYMARKET, "m1", "yes1")]
    # min(bid+tick, ask-tick) → joins existing 0.47 level less its cap: price
    # 0.47+0.01=0.48 capped at ask-tick=0.47 → joins level with 250 ahead.
    assert yes_order.price == pytest.approx(0.47)
    assert yes_order.queue_ahead == pytest.approx(250)

    engine.on_trade(TradeEvent(ts=1010, venue=Venue.POLYMARKET, market_id="m1",
                               outcome_id="yes1", price=0.47, size=200,
                               side="sell"))
    assert yes_order.filled == 0.0  # queue absorbed the whole print
    assert yes_order.queue_ahead == pytest.approx(50)

    engine.on_trade(TradeEvent(ts=1011, venue=Venue.POLYMARKET, market_id="m1",
                               outcome_id="yes1", price=0.47, size=150,
                               side="sell"))
    assert yes_order.filled == pytest.approx(100)  # 50 to queue, 100 to us


def test_leg_risk_liquidation_books_a_loss(config):
    _, det, engine = build(config)
    gap = open_gap(det, engine)
    # Only the YES leg fills…
    engine.on_trade(TradeEvent(ts=1010, venue=Venue.POLYMARKET, market_id="m1",
                               outcome_id="yes1", price=0.47, size=400,
                               side="sell"))
    # …then the gap closes and the attempt is settled.
    engine.on_gap_closed(gap.gap_id, 1015.0)
    maker = [r for r in engine.results if r.policy == Policy.MAKER]
    assert len(maker) == 1
    result = maker[0]
    assert result.outcome == GapOutcomeKind.LEG_RISK
    # Bought 400 @ 0.47, best bid 0.46 (500 deep): lose 1c/share + exit fee.
    assert result.gross_pnl == pytest.approx(400 * (0.46 - 0.47))
    assert result.fees > 0
    assert result.net_pnl < 0


def test_unfilled_expiry_costs_nothing(config):
    _, det, engine = build(config)
    gap = open_gap(det, engine)
    # TTL passes with zero prints: expire via a later book event.
    stale = make_book(Venue.POLYMARKET, "m1", "yes1",
                      [(0.46, 500)], [(0.48, 400)], ts=2000.0)
    det.on_book(stale)
    engine.on_book(stale, 2000.0)
    maker = [r for r in engine.results if r.policy == Policy.MAKER]
    assert len(maker) == 1
    assert maker[0].outcome == GapOutcomeKind.UNFILLED
    assert maker[0].net_pnl == 0.0
    # Reserved cash fully released.
    assert engine.maker_cash[Venue.POLYMARKET] == pytest.approx(
        config.bankroll_per_venue
    )


def test_adverse_selection_crossing_fill(config):
    """Ask collapsing through our bid fills us exactly when price crashes."""
    _, det, engine = build(config)
    gap = open_gap(det, engine)
    crash = make_book(Venue.POLYMARKET, "m1", "yes1",
                      [(0.30, 500)], [(0.35, 400)], ts=1010.0)
    det.on_book(crash)
    engine.on_book(crash, 1010.0)
    attempt = engine.maker_attempts.get(gap.gap_id)
    if attempt is None:  # settled already because other leg never filled
        maker = [r for r in engine.results if r.policy == Policy.MAKER]
        assert maker[0].outcome == GapOutcomeKind.LEG_RISK
    else:
        yes_order = attempt.orders[(Venue.POLYMARKET, "m1", "yes1")]
        assert yes_order.filled == pytest.approx(400)  # filled at 0.47…
        engine.on_gap_closed(gap.gap_id, 1011.0)
        maker = [r for r in engine.results if r.policy == Policy.MAKER]
        result = maker[0]
        assert result.outcome == GapOutcomeKind.LEG_RISK
        # …and immediately liquidated near 0.30: the classic pick-off.
        assert result.net_pnl < -50
