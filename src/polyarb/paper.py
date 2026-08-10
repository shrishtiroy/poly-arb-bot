"""Paper trading engine.

Every detected gap is attempted TWO ways in parallel, with separate virtual
bankrolls, so the report can compare execution styles on identical
opportunities:

TAKER  - cross the spread on all legs instantly at the depth-weighted prices
         the detector just computed, paying each venue's taker fee. This is
         deliberately *optimistic* (assumes perfectly simultaneous fills and
         that we win the race to a visible gap); if taker still loses money
         under these assumptions, it loses harder in reality.

TAKER_DISCIPLINED - the deployable estimate. It does everything TAKER does,
         except it does not get to trade at the detection-time snapshot: it
         waits config.taker_disc_latency_s (decision + order round-trip) and
         then reprices every leg against the book AS IT STANDS at that later
         moment. If the edge closed, the book thinned out, or a leg's book
         disappeared in that window, the attempt is recorded MISSED with zero
         P&L rather than filled at a price that was never actually available.

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
    GapLeg,
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
class PendingTakerDisc:
    """A TAKER_DISCIPLINED attempt waiting out its simulated latency before it
    is allowed to reprice and fill against the book."""

    gap: GapEvent
    ts_exec: float  # gap.ts + config.taker_disc_latency_s


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
        self.pending_taker_disc: list[PendingTakerDisc] = []
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
        #  TAKER            - naive: cross the spread on every detected gap,
        #                      instantly, at the detection-time snapshot.
        #  TAKER_DISCIPLINED - what a real deployable bot would do: it can't
        #                      actually trade at the instant it saw the gap,
        #                      so it queues and fills later against the book
        #                      as it stands after taker_disc_latency_s (see
        #                      _attempt_taker_disciplined). The gap between
        #                      the two rows is the cost of both no discipline
        #                      AND the latency a real bot cannot avoid.
        self._attempt_taker(gap)
        self._attempt_taker_disciplined(gap)
        # MAKER is disabled by default (config.enable_maker): it never completed
        # a two-leg arb in 1,296 live attempts and only ever lost money to
        # leg risk. Nothing else in the maker path runs when no attempt is
        # registered, so the TTL/fill/leg-risk sweeps below simply no-op.
        if self.config.enable_maker:
            self._attempt_maker(gap)

    def _attempt_taker(self, gap: GapEvent) -> None:
        cash = self.taker_cash
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
                order_id=Policy.TAKER.value, gap_id=gap.gap_id,
                policy=Policy.TAKER,
                venue=leg.venue, market_id=leg.market_id,
                outcome_id=leg.outcome_id, category=gap.category,
                price=leg.avg_price, size=shares, fee=fee, rebate_est=0.0,
                ts=gap.ts,
            ))
        gross = gap.gross_edge_per_share * shares
        net = gross - fees
        cash[primary] += net
        self.results.append(GapResult(
            gap_id=gap.gap_id, kind=gap.kind, policy=Policy.TAKER,
            venue=primary, category=gap.category,
            outcome=GapOutcomeKind.CAPTURED, shares=shares,
            gross_pnl=gross, fees=fees, rebate_est=0.0, net_pnl=net,
            ts_open=gap.ts, ts_close=gap.ts,
        ))

    # ---------------------------------------------------- disciplined taker

    def _attempt_taker_disciplined(self, gap: GapEvent) -> None:
        # Skip up front if the detection-time edge doesn't even clear fees -
        # no point waiting out the latency window on a gap that was never
        # worth touching in the first place.
        if gap.net_taker_edge_per_share <= 0:
            return
        self.pending_taker_disc.append(PendingTakerDisc(
            gap=gap, ts_exec=gap.ts + self.config.taker_disc_latency_s,
        ))

    def _reprice_legs(self, legs: list[GapLeg],
                      target_shares: float) -> tuple[list[GapLeg], float] | None:
        """Walk each leg's CURRENT book for target_shares - an honest re-quote
        at execution time, as opposed to trusting the (by now stale)
        detection-time prices baked into `legs`. None if any leg's book
        vanished or depth fell below the executable-size floor."""
        executable = target_shares
        for leg in legs:
            book = self.books.get((leg.venue, leg.market_id, leg.outcome_id))
            if book is None or not book.asks:
                return None
            filled, _ = book.cost_to_buy(target_shares)
            executable = min(executable, filled)
        if executable < self.config.min_executable_shares:
            return None
        repriced: list[GapLeg] = []
        for leg in legs:
            book = self.books[(leg.venue, leg.market_id, leg.outcome_id)]
            filled, cost = book.cost_to_buy(executable)
            if filled + 1e-9 < executable:
                return None
            repriced.append(GapLeg(
                venue=leg.venue, market_id=leg.market_id,
                outcome_id=leg.outcome_id, avg_price=cost / executable,
                shares=executable, levels=book.levels_to_buy(executable),
            ))
        return repriced, executable

    def _execute_taker_disciplined(self, gap: GapEvent, ts: float) -> None:
        repriced = self._reprice_legs(gap.legs, gap.executable_shares)
        if repriced is None:
            self._record_missed(gap, ts)
            return
        legs, executable = repriced
        total_cost = sum(leg.avg_price * executable for leg in legs)
        fees = sum(self._fee_model(leg.venue, leg.market_id)
                   .taker_fee(executable, leg.avg_price) for leg in legs)
        gross_edge = 1.0 - total_cost / executable
        net_edge = gross_edge - fees / executable
        # Re-check discipline against the price the book ACTUALLY offers now -
        # the whole point of this policy is to never book a knowingly-negative
        # trade, and "knowingly" has to mean "at execution time," not detection
        # time, once latency is in the picture.
        if net_edge <= 0:
            self._record_missed(gap, ts)
            return
        cash = self.taker_disc_cash
        primary = legs[0].venue
        shares = executable
        if total_cost > cash[primary]:
            shares *= cash[primary] / total_cost
        if shares < 1:
            self._record_missed(gap, ts)
            return
        # Recompute fees at the (possibly bankroll-scaled-down) final size
        # rather than reusing the executable-size figure above, which was
        # only needed for the discipline check.
        fees_final = 0.0
        for leg in legs:
            fee = self._fee_model(leg.venue, leg.market_id).taker_fee(
                shares, leg.avg_price)
            fees_final += fee
            self.fills.append(PaperFill(
                order_id=Policy.TAKER_DISCIPLINED.value, gap_id=gap.gap_id,
                policy=Policy.TAKER_DISCIPLINED, venue=leg.venue,
                market_id=leg.market_id, outcome_id=leg.outcome_id,
                category=gap.category, price=leg.avg_price, size=shares,
                fee=fee, rebate_est=0.0, ts=ts, note="latency_exec",
            ))
        gross = gross_edge * shares
        net = gross - fees_final
        cash[primary] += net
        self.results.append(GapResult(
            gap_id=gap.gap_id, kind=gap.kind, policy=Policy.TAKER_DISCIPLINED,
            venue=primary, category=gap.category,
            outcome=GapOutcomeKind.CAPTURED, shares=shares,
            gross_pnl=gross, fees=fees_final, rebate_est=0.0, net_pnl=net,
            ts_open=gap.ts, ts_close=ts,
            detail={"edge_at_detect": gap.net_taker_edge_per_share,
                    "edge_at_exec": net_edge},
        ))

    def _record_missed(self, gap: GapEvent, ts: float) -> None:
        self.results.append(GapResult(
            gap_id=gap.gap_id, kind=gap.kind, policy=Policy.TAKER_DISCIPLINED,
            venue=gap.legs[0].venue, category=gap.category,
            outcome=GapOutcomeKind.MISSED, shares=0.0,
            gross_pnl=0.0, fees=0.0, rebate_est=0.0, net_pnl=0.0,
            ts_open=gap.ts, ts_close=ts,
            detail={"edge_at_detect": gap.net_taker_edge_per_share},
        ))

    def _sweep_taker_disc(self, ts: float, force: bool = False) -> None:
        ready = [p for p in self.pending_taker_disc
                if force or ts >= p.ts_exec]
        if not ready:
            return
        ready_ids = {id(p) for p in ready}
        self.pending_taker_disc = [p for p in self.pending_taker_disc
                                   if id(p) not in ready_ids]
        for pending in ready:
            self._execute_taker_disciplined(pending.gap, pending.ts_exec)

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
        self._sweep_taker_disc(ts)

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
        # Collection ended before some disciplined-taker latency windows
        # elapsed; execute them now against the final book rather than losing
        # the attempt entirely.
        self._sweep_taker_disc(ts, force=True)

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
