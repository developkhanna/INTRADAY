"""The dashboard, the live loop and the bootstrap all open the ledger at once."""

from __future__ import annotations

import multiprocessing as mp
from pathlib import Path

from intraday.live.ledger import Ledger


def open_ledger(path: str, results) -> None:
    try:
        Ledger(path=Path(path)).query("SELECT COUNT(*) AS n FROM predictions")
        results.append("ok")
    except Exception as exc:  # noqa: BLE001 - the failure message is the point
        results.append(f"{type(exc).__name__}: {exc}")


def test_several_processes_can_open_the_ledger_at_the_same_moment(tmp_path):
    """Regression: two processes creating the schema raced into a write-write conflict."""
    path = str(tmp_path / "ledger.duckdb")
    with mp.Manager() as manager:
        results = manager.list()
        workers = [mp.Process(target=open_ledger, args=(path, results)) for _ in range(4)]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(timeout=60)
        assert list(results) == ["ok"] * 4


def test_reads_go_through_one_shared_schema_creation(tmp_path):
    path = tmp_path / "ledger.duckdb"
    first = Ledger(path=path)
    second = Ledger(path=path)

    assert first.query("SELECT COUNT(*) AS n FROM predictions")["n"].iloc[0] == 0
    assert second.query("SELECT COUNT(*) AS n FROM outcomes")["n"].iloc[0] == 0
