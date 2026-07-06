"""polyarb CLI.

    polyarb collect --hours 24            # live, public data only
    polyarb collect --replay tests/fixtures/replay   # deterministic replay
    polyarb report                        # cross-venue comparison table
    polyarb decide                        # DEPLOY / NO-DEPLOY verdict
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Optional

import typer

from .config import Config
from .recorder import Recorder

app = typer.Typer(add_completion=False, help=__doc__)

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s")


def _load(config_path: Optional[Path]) -> Config:
    return Config.load(config_path)


@app.command()
def collect(
    hours: float = typer.Option(1.0, help="Live collection duration."),
    replay: Optional[Path] = typer.Option(
        None, help="Replay a JSONL fixture file/dir instead of going live."),
    config: Optional[Path] = typer.Option(None, help="YAML config override."),
    db: Optional[Path] = typer.Option(None, help="SQLite output path."),
):
    """Collect gaps and paper-trade them (live or replay)."""
    cfg = _load(config)
    recorder = Recorder(db or cfg.db_path)
    from .runner import run_live, run_replay

    try:
        if replay is not None:
            runner = run_replay(cfg, replay, recorder)
        else:
            runner = asyncio.run(run_live(cfg, hours, recorder))
    finally:
        recorder.close()
    typer.echo(
        f"done: {len(runner.detector.partitions)} partitions watched, "
        f"results written to {db or cfg.db_path}"
    )


@app.command(name="gen-fixture")
def gen_fixture(
    out_dir: Path = typer.Argument(Path("tests/fixtures/replay"),
                                   help="Output directory."),
):
    """Regenerate the deterministic demo replay fixture."""
    from .fixture_gen import generate

    replay_file = generate(out_dir)
    typer.echo(f"wrote {replay_file}")


@app.command()
def report(
    db: Optional[Path] = typer.Option(None, help="SQLite path."),
    config: Optional[Path] = typer.Option(None, help="YAML config override."),
):
    """Print the cross-venue comparison table."""
    cfg = _load(config)
    from .report import render_report

    typer.echo(render_report(db or cfg.db_path))


@app.command()
def decide(
    db: Optional[Path] = typer.Option(None, help="SQLite path."),
    config: Optional[Path] = typer.Option(None, help="YAML config override."),
):
    """Emit the DEPLOY / NO-DEPLOY verdict."""
    cfg = _load(config)
    from .report import decide as decide_fn

    typer.echo(decide_fn(db or cfg.db_path, cfg))


if __name__ == "__main__":
    app()
