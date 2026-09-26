"""Prediction ledger.

Every prediction the system makes is written down before the outcome is known,
with the exact feature snapshot and model version. Later, the real outcome is
attached to the same row. This is what makes the accuracy page honest: it is
scored against predictions that were recorded in advance, not re-derived after
the fact.
"""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import duckdb
import pandas as pd

from intraday.config import LEDGER_DB, ensure_dirs

SCHEMA = """
CREATE TABLE IF NOT EXISTS predictions (
    prediction_id VARCHAR PRIMARY KEY,
    created_at TIMESTAMPTZ NOT NULL,
    bar_timestamp TIMESTAMPTZ NOT NULL,
    symbol VARCHAR NOT NULL,
    horizon_minutes INTEGER NOT NULL,
    model_version VARCHAR NOT NULL,
    model_status VARCHAR NOT NULL,
    probability DOUBLE,
    expected_return DOUBLE,
    expected_mfe DOUBLE,
    expected_mae DOUBLE,
    target_pct DOUBLE,
    stop_pct DOUBLE,
    cost_pct DOUBLE,
    expected_value_pct DOUBLE,
    action VARCHAR,
    reason VARCHAR,
    eli5 VARCHAR,
    price_at_prediction DOUBLE,
    features JSON
);

CREATE TABLE IF NOT EXISTS outcomes (
    prediction_id VARCHAR PRIMARY KEY,
    resolved_at TIMESTAMPTZ NOT NULL,
    realized_return DOUBLE,
    realized_mfe DOUBLE,
    realized_mae DOUBLE,
    target_before_stop INTEGER,
    minutes_to_resolution INTEGER
);
"""


# The dashboard, the live loop and the bootstrap all open this file. DuckDB
# rejects two connections changing the catalog at the same moment, so the
# schema is created once per process and retried if another process is mid-way
# through creating it.
_SCHEMA_LOCK = threading.Lock()
_SCHEMA_DONE: set[str] = set()
_RETRY_DELAYS = (0.1, 0.3, 0.7)


@dataclass
class Ledger:
    path: Path = LEDGER_DB

    def __post_init__(self) -> None:
        ensure_dirs()
        self._ensure_schema()

    def connect(self) -> duckdb.DuckDBPyConnection:
        return duckdb.connect(str(self.path))

    def _ensure_schema(self) -> None:
        key = str(self.path)
        with _SCHEMA_LOCK:
            if key in _SCHEMA_DONE:
                return
            self._with_retry(lambda con: con.execute(SCHEMA))
            _SCHEMA_DONE.add(key)

    def _with_retry(self, work: Callable[[duckdb.DuckDBPyConnection], object]) -> object:
        last: duckdb.Error | None = None
        for delay in (*_RETRY_DELAYS, None):
            try:
                with self.connect() as con:
                    return work(con)
            except duckdb.Error as exc:
                last = exc
                if delay is None:
                    break
                time.sleep(delay)
        raise last if last else RuntimeError("ledger unavailable")

    def record(self, rows: pd.DataFrame) -> int:
        """Insert predictions. `features` is stored as JSON for later audit."""
        if rows.empty:
            return 0
        frame = rows.copy()
        if "features" in frame:
            frame["features"] = frame["features"].apply(
                lambda value: value if isinstance(value, str) else json.dumps(value)
            )

        def work(con: duckdb.DuckDBPyConnection) -> None:
            con.register("incoming", frame)
            con.execute(
                "INSERT OR IGNORE INTO predictions SELECT "
                + ", ".join(self._columns(con, "predictions", frame))
                + " FROM incoming"
            )

        self._with_retry(work)
        return len(frame)

    def attach_outcome(self, rows: pd.DataFrame) -> int:
        if rows.empty:
            return 0

        def work(con: duckdb.DuckDBPyConnection) -> None:
            con.register("incoming_outcomes", rows)
            con.execute(
                "INSERT OR REPLACE INTO outcomes SELECT "
                + ", ".join(self._columns(con, "outcomes", rows))
                + " FROM incoming_outcomes"
            )

        self._with_retry(work)
        return len(rows)

    def query(self, sql: str, params: list | None = None) -> pd.DataFrame:
        """Read the ledger, waiting out a writer that holds the catalog."""
        frame = self._with_retry(lambda con: con.execute(sql, params or []).fetch_df())
        assert isinstance(frame, pd.DataFrame)
        return frame

    def pending(self, now: pd.Timestamp) -> pd.DataFrame:
        """Predictions whose horizon has elapsed but which have no outcome yet."""
        return self.query(
            """
            SELECT p.*
            FROM predictions p
            LEFT JOIN outcomes o USING (prediction_id)
            WHERE o.prediction_id IS NULL
              AND p.bar_timestamp + INTERVAL 1 MINUTE * p.horizon_minutes <= ?
            ORDER BY p.bar_timestamp
            """,
            [now],
        )

    def scored(self) -> pd.DataFrame:
        return self.query("SELECT * FROM predictions JOIN outcomes USING (prediction_id)")

    @staticmethod
    def _columns(con: duckdb.DuckDBPyConnection, table: str, frame: pd.DataFrame) -> list[str]:
        """Align incoming columns to the table order, NULL for anything missing."""
        table_columns = [row[0] for row in con.execute(f"DESCRIBE {table}").fetchall()]
        return [c if c in frame.columns else "NULL" for c in table_columns]
