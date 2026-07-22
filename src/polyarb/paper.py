"""Paper trading engine.

Every detected gap is attempted TWO ways in parallel, with separate virtual
bankrolls, so the report can compare execution styles on identical
opportunities:

TAKER  - cross the spread on all legs instantly at the depth-weighted prices
         the detector just computed, paying each venue's taker fee. This is
         deliberately *optimistic* (assumes perfectly simultaneous fills and
         that we win the race to a visible gap); if taker still loses money
         under these assumptions, it loses harder in reality.

MAKER  - rest a limit buy on every leg one tick above the current best bid and
         wait, modeling the FIFO queue *pessimistically*:
           * joining an existing price level puts the level's full visible
             size ahead of us;
           * we advance only when prints actually occur at (or through) our
             price;
           * a book that crosses down through our bid fills us at our limit -
             which is exactly the adverse-selection case: we get filled
             because the price just collapsed past us.
         If the gap closes or the TTL expires with only some legs filled, the
         filled legs are liquidated at market (paying taker fees) and the loss
         is booked as LEG_RISK. Maker rebates are credited only as a separately
         reported *upper-bound estimate* (the real pool is pro-rata to maker
         volume share, unknowable in advance).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from .config import Config
from .fees import FeeModel, fee_model_for
from .models import (
    GapEvent,
    GapOutcomeKind,
    GapResult,
    Market,
    OrderBook,
    PaperFill,
    PaperOrder,
    Policy,
    TradeEvent,
    Venue,
)

log = logging.getLogger(__name__)

BookKey = tuple[Venue, str, str]


@dataclass
class MakerAttempt:
    gap: GapEvent
    orders: dict[BookKey, PaperOrder]
    fills: list[PaperFill] = field(default_factory=list)
    reserved: dict[Venue, float] = field(default_factory=dict)

    @property
    def all_filled(self) -> bool:
        return all(o.open_size <= 1e-9 for o in self.orders.values())

    @property
    def any_filled(self) -> bool:
        return any(o.filled > 1e-9 for o in self.orders.values())


class PaperEngine:
    def __init__(self, config: Config, markets: dict[tuple[Venue, str], Market],
                 books: dict[BookKey, OrderBook]):
        self.config = config
        self.markets = markets
        self.books = books  # shared with GapDetector (latest snapshots)
        self.taker_cash: dict[Venue, float] = {v: config.bankroll_per_venue for v in Venue}
        self.taker_disc_cash: dict[Venue, float] = {v: config.bankroll_per_venue for v in Venue}
        self.maker_cash: dict[Venue, float] = {v: config.bankroll_per_venue for v in Venue}
        self.maker_attempts: dict[str, MakerAttempt] = {}  # gap_id →
        self.results: list[GapResult] = []
        self.fills: list[PaperFill] = []

    def _fee_model(self, venue: Venue, market_id: str) -> FeeModel:
        market = self.markets.get((venue, market_id))
        if market is None:
            # Cross-venue mapping legs may reference markets outside the
            # discovery cap; fall back to a bare market with schedule defaults.
            from .models import Category, Outcome
            market = Market(
                venue=venue, market_id=market_id, question="",
                category=Category.OTHER,
                outcomes=[Outcome(outcome_id="x", name="x")] * 2,
            )
        return fee_model_for(market)

    # ------------------------------------------------------------------ gaps

    def on_gap(self, gap: GapEvent) -> None:
        # Two taker policies on the SAME gap, so the report can contrast them:
        #  TAKER            - naive: cross the spread on every detected gap.
        #  TAKER_DISCIPLINED - only cross when net edge (after fees) > 0. This is
        #                      what a real deployable bot would do; the gap
        #                      between the two rows is the cost of no discipline.
        self._attempt_taker(gap, Policy.TAKER)
        self._attempt_taker(gap, Policy.TAKER_DISCIPLINED)
        self._attempt_maker(gap)

    def _attempt_taker(self, gap: GapEvent, policy: Policy) -> None:
        # Disciplined taker skips gaps that don't clear fees - it never books a
        # knowingly-negative trade, so its cash only sees profitable captures.
        if (policy is Policy.TAKER_DISCIPLINED
                and gap.net_taker_edge_per_share <= 0):
            return
        cash = (self.taker_cash if policy is Policy.TAKER
                else self.taker_disc_cash)
        shares = gap.executable_shares
        cost = sum(leg.avg_price for leg in gap.legs) * shares
        primary = gap.legs[0].venue
        if cost > cash[primary]:
            shares *= cash[primary] / cost
        if shares < 1:
            return
        fees = 0.0
        for leg in gap.legs:
            model = self._fee_model(leg.venue, leg.market_id)
            fee = model.taker_fee(shares, leg.avg_price)
            fees += fee
            self.fills.append(PaperFill(
                order_id=policy.value, gap_id=gap.gap_id, policy=policy,
                venue=leg.venue, market_id=leg.market_id,
                outcome_id=leg.outcome_id, category=gap.category,
                price=leg.avg_price, size=shares, fee=fee, rebate_est=0.0,
                ts=gap.ts,
            ))
        gross = gap.gross_edge_per_share * shares
        net = gross - fees
        cash[primary] += net
        self.results.append(GapResult(
            gap_id=gap.gap_id, kind=gap.kind, policy=policy,
            venue=primary, category=gap.category,
            outcome=GapOutcomeKind.CAPTURED, shares=shares,
            gross_pnl=gross, fees=fees, rebate_est=0.0, net_pnl=net,
            ts_open=gap.ts, ts_close=gap.ts,
        ))

    def _attempt_maker(self, gap: GapEvent) -> None:
        tick = self.config.maker_price_improve_tick
        orders: dict[BookKey, PaperOrder] = {}
        prices: dict[BookKey, float] = {}
        for leg in gap.legs:
            key: BookKey = (leg.venue, leg.market_id, leg.outcome_id)
            book = self.books.get(key)
            if book is None or book.best_ask is None:
                return
            best_bid = book.best_bid if book.best_bid is not None else tick
            price = min(best_bid + tick, book.best_ask - tick)
            price = max(price, tick)
            prices[key] = round(price, 4)
        # Only rest the set if the limit prices still lock an edge when all fill.
        if sum(prices.values()) > 1.0 - self.config.min_gross_edge:
            return
        shares = gap.executable_shares
        # Disciplined maker: skip if the locked profit would be net negative
        # after maker fees (never rest a knowingly-losing order).
        gross_edge = 1.0 - sum(prices.values())
        maker_fees = sum(
            self._fee_model(key[0], key[1]).maker_fee(1.0, price)
            for key, price in prices.items()
        )
        if gross_edge - maker_fees <= 0:
            return
        needed: dict[Venue, float] = {}
        for key, price in prices.items():
            needed[key[0]] = needed.get(key[0], 0.0) + price * shares
        if any(self.maker_cash[v] < amt for v, amt in needed.items()):
            return
        for v, amt in needed.items():
            self.maker_cash[v] -= amt
        for leg in gap.legs:
            key = (leg.venue, leg.market_id, leg.outcome_id)
            book = self.books[key]
            price = prices[key]
            # Pessimistic FIFO: joining an existing level queues us behind its
            # entire visible size; a fresh (improving) level starts clean.
            queue_ahead = book.size_at("bid", price)
            orders[key] = PaperOrder(
                gap_id=gap.gap_id, policy=Policy.MAKER, venue=leg.venue,
                market_id=leg.market_id, outcome_id=leg.outcome_id,
                category=gap.category, price=price, size=shares,
                ts_placed=gap.ts, queue_ahead=queue_ahead,
            )
        self.maker_attempts[gap.gap_id] = MakerAttempt(
            gap=gap, orders=orders, reserved=needed,
        )

    # ----------------------------------------------------------------- feeds

    def on_trade(self, trade: TradeEvent) -> None:
        key: BookKey = (trade.venue, trade.market_id, trade.outcome_id)
        for attempt in list(self.maker_attempts.values()):
            order = attempt.orders.get(key)
            if order is None or order.open_size <= 1e-9:
                continue
            if trade.side != "sell":
                continue  # only aggressive sellers hit resting bids
            if trade.price > order.price + 1e-9:
                continue  # print above our level: doesn't reach us
            size = trade.size
            if abs(trade.price - order.price) <= 1e-9:
                # At our level: FIFO - queue ahead absorbs the print first.
                absorbed = min(order.queue_ahead, size)
                order.queue_ahead -= absorbed
                size -= absorbed
            # Below our level: price priority means we'd have filled first.
            fill_size = min(order.open_size, size)
            if fill_size > 0:
                self._fill(attempt, order, fill_size, trade.ts, note="print")

    def on_book(self, book: OrderBook, ts: float) -> None:
        """Crossing fills: if the ask side collapses to (or through) our bid,
        we are filled at our limit - the adverse-selection fill."""
        key: BookKey = (book.venue, book.market_id, book.outcome_id)
        for attempt in list(self.maker_attempts.values()):
            order = attempt.orders.get(key)
            if order is None or order.open_size <= 1e-9:
                continue
            if book.best_ask is not None and book.best_ask <= order.price + 1e-9:
                self._fill(attempt, order, order.open_size, ts, note="crossed")
        self._expire(ts)

    def _fill(self, attempt: MakerAttempt, order: PaperOrder, size: float,
              ts: float, note: str) -> None:
        model = self._fee_model(order.venue, order.market_id)
        fee = model.maker_fee(size, order.price)
        rebate = model.maker_rebate_estimate(size, order.price)
        order.filled += size
        fill = PaperFill(
            order_id=order.order_id, gap_id=order.gap_id, policy=Policy.MAKER,
            venue=order.venue, market_id=order.market_id,
            outcome_id=order.outcome_id, category=order.category,
            price=order.price, size=size, fee=fee, rebate_est=rebate,
            ts=ts, note=note,
        )
        attempt.fills.append(fill)
        self.fills.append(fill)
        if attempt.all_filled:
            self._settle(attempt, ts, GapOutcomeKind.CAPTURED)

    # ------------------------------------------------------------- lifecycle

    def on_gap_closed(self, gap_id: str, ts: float) -> None:
        attempt = self.maker_attempts.get(gap_id)
        if attempt is None:
            return
        self._settle(
            attempt, ts,
            GapOutcomeKind.LEG_RISK if attempt.any_filled else GapOutcomeKind.UNFILLED,
        )

    def _expire(self, ts: float) -> None:
        ttl = self.config.maker_order_ttl_s
        for attempt in list(self.maker_attempts.values()):
            if ts - attempt.gap.ts < ttl:
                continue
            self._settle(
                attempt, ts,
                GapOutcomeKind.LEG_RISK if attempt.any_filled else GapOutcomeKind.UNFILLED,
            )

    def finalize(self, ts: float) -> None:
        """End of run: force-settle whatever is still resting."""
        for attempt in list(self.maker_attempts.values()):
            self._settle(
                attempt, ts,
                GapOutcomeKind.LEG_RISK if attempt.any_filled else GapOutcomeKind.UNFILLED,
            )

    def _settle(self, attempt: MakerAttempt, ts: float,
                outcome: GapOutcomeKind) -> None:
        gap = attempt.gap
        self.maker_attempts.pop(gap.gap_id, None)
        for venue, amount in attempt.reserved.items():
            self.maker_cash[venue] += amount

        gross = 0.0
        fees = sum(f.fee for f in attempt.fills)
        rebate = sum(f.rebate_est for f in attempt.fills)
        shares = min((o.filled for o in attempt.orders.values()), default=0.0)

        if outcome is GapOutcomeKind.CAPTURED:
            # Complete partition: every matched share-set pays $1 at resolution.
            cost = sum(o.price * o.filled for o in attempt.orders.values())
            gross = shares * 1.0 - cost
        elif outcome is GapOutcomeKind.LEG_RISK:
            # Liquidate whatever filled at the current bids, paying taker fees.
            for key, order in attempt.orders.items():
                if order.filled <= 1e-9:
                    continue
                book = self.books.get(key)
                model = self._fee_model(order.venue, order.market_id)
                if book is not None and book.bids:
                    sold, proceeds = book.proceeds_to_sell(order.filled)
                    stranded = order.filled - sold
                    # Unsellable remainder: value at 0 (worst case honesty).
                    exit_fee = model.taker_fee(sold, proceeds / sold if sold else 0.0)
                    fees += exit_fee
                    gross += proceeds - order.price * order.filled
                    if stranded > 0:
                        gross -= order.price * stranded
                else:
                    gross -= order.price * order.filled  # no bids: total loss
            shares = max((o.filled for o in attempt.orders.values()), default=0.0)

        net = gross - fees
        for venue in attempt.reserved:
            self.maker_cash[venue] += net / max(len(attempt.reserved), 1)
        self.results.append(GapResult(
            gap_id=gap.gap_id, kind=gap.kind, policy=Policy.MAKER,
            venue=gap.legs[0].venue, category=gap.category, outcome=outcome,
            shares=shares, gross_pnl=gross, fees=fees, rebate_est=rebate,
            net_pnl=net, ts_open=gap.ts, ts_close=ts,
            # Resting limit price per leg, so the dashboard can show what price
            # the maker rested at even when the order never filled.
            detail={o.outcome_id: o.price for o in attempt.orders.values()},
        ))
