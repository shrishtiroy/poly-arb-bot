"""Full pipeline on the synthetic 8-day fixture: collect(replay) → report → decide."""

import pytest

from polyarb.config import Config, DecisionCriteria
from polyarb.fixture_gen import generate
from polyarb.recorder import Recorder
from polyarb.report import decide, render_report
from polyarb.runner import run_replay


@pytest.fixture(scope="module")
def replay_db(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("replay")
    generate(tmp)
    db_path = tmp / "test.sqlite"
    config = Config(
        db_path=str(db_path),
        event_mappings_path=str(tmp / "mappings.yaml"),
    )
    recorder = Recorder(db_path)
    runner = run_replay(config, tmp / "demo.jsonl", recorder)
    recorder.close()
    return db_path, config, runner


def test_replay_processes_all_partitions(replay_db):
    _, _, runner = replay_db
    # 4 binary partitions + 1 cross-venue mapping
    assert len(runner.detector.partitions) == 5


def test_report_shows_fee_asymmetry(replay_db):
    db_path, _, _ = replay_db
    text = render_report(db_path)
    assert "polymarket" in text and "kalshi" in text and "novig" in text
    assert "cross_venue" in text

    # The core adversarial claim, reproduced end-to-end: identical 3c gaps are
    # net-positive for takers on sports fees but net-NEGATIVE on crypto fees.
    from polyarb.report import load_cells
    cells, _ = load_cells(db_path)
    # Cross-venue attempts must live in their own rows, not inflate a venue's.
    assert any(c.kind == "cross_venue" for c in cells)
    by_key = {(c.venue, c.category, c.policy): c for c in cells
              if c.kind == "binary"}
    assert by_key[("polymarket", "sports", "taker")].net_pnl > 0
    assert by_key[("polymarket", "crypto", "taker")].net_pnl < 0
    # Maker on the same gaps beats taker where it fills (no fee + wider capture)
    assert (by_key[("polymarket", "sports", "maker")].net_pnl
            > by_key[("polymarket", "sports", "taker")].net_pnl)
    # Fee-free Novig maker ties/beats everyone on identical gaps.
    novig_maker = by_key[("novig", "sports", "maker")]
    assert novig_maker.net_pnl >= by_key[("kalshi", "sports", "maker")].net_pnl
    # Leg-risk episodes booked losses somewhere.
    assert any(c.n_leg_risk > 0 for c in cells)


def test_decide_deploys_on_qualifying_data(replay_db):
    db_path, config, _ = replay_db
    verdict = decide(db_path, config)
    assert "VERDICT: DEPLOY" in verdict
    assert "policy=maker" in verdict


def test_decide_refuses_thin_history(replay_db):
    db_path, config, _ = replay_db
    strict = config.model_copy(deep=True)
    strict.decision = DecisionCriteria(min_days_of_data=30)
    verdict = decide(db_path, strict)
    assert "VERDICT: NO-DEPLOY" in verdict
    assert "day(s) of data" in verdict


def test_gap_lifetimes_recorded(replay_db):
    db_path, _, _ = replay_db
    import sqlite3

    conn = sqlite3.connect(db_path)
    (n,) = conn.execute("SELECT COUNT(*) FROM gap_lifetimes").fetchone()
    (avg,) = conn.execute(
        "SELECT AVG(ts_close - ts_open) FROM gap_lifetimes"
    ).fetchone()
    conn.close()
    assert n > 0
    assert avg == pytest.approx(20.0, abs=1.0)  # episodes close 20s after open
