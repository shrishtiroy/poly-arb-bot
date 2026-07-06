"""Gap detection: depth-honesty and open/close tracking."""

import pytest

from conftest import make_book, make_market
from polyarb.gaps import GapDetector, build_partitions, load_mappings
from polyarb.models import Category, GapKind, Venue


def make_detector(config, markets):
    return GapDetector(
        config=config,
        markets={(m.venue, m.market_id): m for m in markets},
        partitions=build_partitions(markets, []),
    )


def feed(detector, venue, market_id, outcome_id, bids, asks, ts=1000.0):
    return detector.on_book(make_book(venue, market_id, outcome_id, bids, asks, ts))


def test_binary_gap_detected_with_depth(config):
    market = make_market()
    det = make_detector(config, [market])
    feed(det, Venue.POLYMARKET, "m1", "yes1", [(0.46, 300)], [(0.48, 400)])
    opened, closed = feed(det, Venue.POLYMARKET, "m1", "no1",
                          [(0.47, 300)], [(0.49, 400)])
    assert len(opened) == 1
    gap = opened[0]
    assert gap.kind == GapKind.BINARY
    assert gap.executable_shares == pytest.approx(400)
    assert gap.gross_edge_per_share == pytest.approx(0.03)
    # sports taker fees on both legs: 0.03*(0.48*0.52 + 0.49*0.51)
    assert gap.net_taker_edge_per_share == pytest.approx(
        0.03 - 0.03 * (0.48 * 0.52 + 0.49 * 0.51)
    )
    assert not closed


def test_thin_book_gap_rejected(config):
    market = make_market()
    det = make_detector(config, [market])
    feed(det, Venue.POLYMARKET, "m1", "yes1", [(0.40, 50)], [(0.45, 50)])
    opened, _ = feed(det, Venue.POLYMARKET, "m1", "no1",
                     [(0.40, 50)], [(0.45, 50)])
    assert opened == []  # 10c edge but only 50 shares deep: noise


def test_depth_weighted_price_kills_top_of_book_illusion(config):
    market = make_market()
    det = make_detector(config, [market])
    # Top of book shows 0.44 but only 10 shares; the real fill is at 0.51.
    feed(det, Venue.POLYMARKET, "m1", "yes1",
         [(0.40, 300)], [(0.44, 10), (0.51, 500)])
    opened, _ = feed(det, Venue.POLYMARKET, "m1", "no1",
                     [(0.40, 300)], [(0.49, 500)])
    assert opened == []  # 0.44+0.49 looks like arb; walked cost is ~1.0


def test_gap_closes_when_books_tighten(config):
    market = make_market()
    det = make_detector(config, [market])
    feed(det, Venue.POLYMARKET, "m1", "yes1", [(0.46, 300)], [(0.48, 400)])
    opened, _ = feed(det, Venue.POLYMARKET, "m1", "no1",
                     [(0.47, 300)], [(0.49, 400)])
    gap_id = opened[0].gap_id
    _, closed = feed(det, Venue.POLYMARKET, "m1", "yes1",
                     [(0.46, 300)], [(0.52, 400)], ts=1005.0)
    assert closed == [("bin:polymarket:m1", gap_id, 1005.0)]


def test_multi_outcome_partition(config):
    markets = [
        make_market(market_id=f"g{i}", yes_id=f"y{i}", no_id=f"n{i}")
        for i in range(3)
    ]
    for m in markets:
        m.group_id = "grp"
        m.category = Category.POLITICS
    det = make_detector(config, markets)
    feed(det, Venue.POLYMARKET, "g0", "y0", [(0.30, 300)], [(0.32, 400)])
    feed(det, Venue.POLYMARKET, "g1", "y1", [(0.30, 300)], [(0.31, 400)])
    opened, _ = feed(det, Venue.POLYMARKET, "g2", "y2",
                     [(0.30, 300)], [(0.33, 400)])
    multi = [g for g in opened if g.kind == GapKind.MULTI]
    assert len(multi) == 1
    assert multi[0].gross_edge_per_share == pytest.approx(1 - 0.96)


def test_cross_venue_mapping(config, tmp_path):
    mapping_file = tmp_path / "mappings.yaml"
    mapping_file.write_text(
        """
cross_venue:
  - name: "test game"
    category: sports
    legs:
      - {venue: polymarket, market_id: "m1", outcome_id: "yes1"}
      - {venue: kalshi, market_id: "K1", outcome_id: "K1:no"}
"""
    )
    pm = make_market()
    kalshi = make_market(venue=Venue.KALSHI, market_id="K1",
                         yes_id="K1:yes", no_id="K1:no")
    det = GapDetector(
        config=config,
        markets={(m.venue, m.market_id): m for m in (pm, kalshi)},
        partitions=build_partitions([pm, kalshi], load_mappings(mapping_file)),
    )
    feed(det, Venue.POLYMARKET, "m1", "yes1", [(0.45, 300)], [(0.47, 400)])
    feed(det, Venue.POLYMARKET, "m1", "no1", [(0.50, 300)], [(0.56, 400)])
    opened, _ = feed(det, Venue.KALSHI, "K1", "K1:no",
                     [(0.48, 300)], [(0.50, 400)])
    xv = [g for g in opened if g.kind == GapKind.CROSS_VENUE]
    assert len(xv) == 1
    assert xv[0].gross_edge_per_share == pytest.approx(1 - 0.97)
    venues = {leg.venue for leg in xv[0].legs}
    assert venues == {Venue.POLYMARKET, Venue.KALSHI}
