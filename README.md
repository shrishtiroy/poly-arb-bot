# polyarb — prediction-market arb feasibility harness

A **measurement-first paper trader** for prediction-market arbitrage across
**Polymarket, Kalshi, and Novig**. It watches public market data, detects
mispricings, paper-trades every one of them two ways at once (taker vs maker),
and ends with an explicit **DEPLOY / NO-DEPLOY** verdict telling you whether a
live bot is worth building — and on which venue.

## Why this exists (read before dreaming of $50k/month)

This project started from a viral "Polymarket arb blueprint" thread. The
thread's *facts* are right and encoded here:

- Taker fees follow `fee = shares × rate × p(1-p)`, peaking at 50¢ — the
  exact price region where arb gaps live. Polymarket rates by category
  (crypto 0.07 … sports 0.03, geopolitics free); Kalshi ~0.07 with per-order
  round-up; Novig is commission-free.
- Makers pay zero on Polymarket and get 20–25% of taker fees rebated pro-rata
  to filled maker volume. Kalshi *charges* makers on some series instead.

The thread's *conclusion* ("rest limit orders, collect the gap plus rebates")
is what this harness stress-tests, because it quietly swaps arbitrage for
market making:

- **Leg risk** — a resting two-leg "arb" that fills one leg is a naked
  directional bet, not an arb.
- **Adverse selection** — your resting order fills exactly when the price
  moves through it.
- **FIFO queues** — fills require winning queue position against professional
  makers.
- **Depth illusions** — most visible "gaps" have no executable size behind
  them.

All four failure modes are modeled *pessimistically* here (see below), while
taker execution is modeled *optimistically*. If a strategy can't make paper
money under those tilted rules, it will not make real money.

## What it measures

For every detected gap — three shapes, all depth-weighted for a target
notional, never top-of-book:

| strategy | meaning |
|---|---|
| `binary` | YES + NO on one market cost < $1 |
| `multi` | all outcomes of a neg-risk group cost < $1 |
| `cross_venue` | complementary outcomes on two venues cost < $1 (via `mappings.yaml`) |

…the paper engine attempts it two ways with separate virtual bankrolls:

- **TAKER** — cross the spread on all legs instantly at walked-book prices,
  paying each venue's real taker fee. Optimistic by design (assumes
  simultaneous fills and winning the race).
- **MAKER** — rest limit buys one tick above best bid and wait, with a
  pessimistic FIFO model: joining a level queues you behind its full visible
  size; you advance only on actual prints; a book that crosses your bid fills
  you exactly when the price collapses (adverse selection); gaps that close
  with one leg filled are liquidated at market and booked as `leg_risk`.
  Polymarket rebates are credited only as a separately-reported *upper bound*.

## Quickstart (demo replay, no network needed)

```bash
pip install -e ".[dev]"
pytest                                     # 34 tests

polyarb collect --replay tests/fixtures/replay/demo.jsonl \
                --config tests/fixtures/replay/config.yaml --db demo.sqlite
polyarb report --db demo.sqlite
polyarb decide --db demo.sqlite
```

The bundled 8-day synthetic fixture reproduces the fee asymmetry end-to-end:
identical 3¢ gaps are net-positive for takers on sports fees, net-**negative**
on crypto and Kalshi fees, free money on Novig, and maker execution beats
taker everywhere it actually fills — while ~17% of maker attempts end in
leg-risk losses. Regenerate it with `polyarb gen-fixture`.

## Live measurement (run on your own machine)

> The development sandbox's egress proxy blocks all three venues, so live
> collection was **not** exercised in CI; the adapters' parsing is
> fixture-tested. Expect to smoke-test the endpoints on first run.

```bash
polyarb collect --hours 24        # then repeat daily for a week+
polyarb report
polyarb decide
```

- **Polymarket** — no credentials needed (public Gamma + CLOB websocket).
- **Kalshi** — no credentials needed (public REST polling).
- **Novig** — set `NOVIG_CLIENT_ID` / `NOVIG_CLIENT_SECRET` (read-only OAuth
  client credentials from the developer settings; see docs.novig.com). The
  endpoint paths are env-overridable (`NOVIG_API_BASE`, `NOVIG_MARKETS_PATH`,
  `NOVIG_ORDERBOOK_PATH`) — confirm them against the docs on first run, since
  they could not be verified from the build environment.

Cross-venue detection needs a hand-written `mappings.yaml` pairing *truly
equivalent* markets (identical resolution criteria!) across venues — see
`tests/fixtures/replay/mappings.yaml` for the format.

## The verdict

`polyarb decide` grants **DEPLOY(venue, category, strategy, policy)** only if a
cell clears **all** of: ≥7 days of data, ≥30 captures, positive net P&L after
fees, and still positive after deleting its single best day. Otherwise it says
**NO-DEPLOY** and tells you the binding reason per cell (e.g. "gaps exist but
p90 lifetime 0.4s — latency-losing" is what you should expect to see).
Jurisdiction caveats are attached to every verdict (Novig is a state-by-state
sweepstakes model; Kalshi is CFTC-regulated; Polymarket US for US users).

## Safety properties

- **No trading code exists in this phase.** The adapter interface has no
  order-placement method; nothing here can spend money even if misconfigured.
- **No private keys, ever.** The only credentials are read-only API tokens
  from env vars. In particular, do **not** install `py_clob_client_v2` from
  social-media threads — it is not Polymarket's official client
  (`py-clob-client`), and "paste your private key into my package" is the
  wallet-drainer playbook.
- Fee rates are read from venue APIs when exposed and fall back to the
  published 2026-07 schedules **with a logged warning** — never silently.

## Layout

```
src/polyarb/
  venues/        polymarket.py, kalshi.py, novig.py behind one read-only interface
  fees.py        p(1-p) fee curves, per-venue asymmetries (rebates vs maker fees)
  gaps.py        depth-weighted partition detector (binary / multi / cross-venue)
  paper.py       dual taker/maker paper engine, FIFO queue + leg-risk model
  recorder.py    SQLite persistence
  report.py      comparison table + DEPLOY/NO-DEPLOY decision engine
  runner.py      event loop; identical for live and --replay
  fixture_gen.py deterministic demo scenario
```
