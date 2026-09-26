"""Prediction ledger.

Every prediction the system makes is written down before the outcome is known,
with the exact feature snapshot and model version. Later, the real outcome is
attached to the same row. This is what makes the accuracy page honest: it is
scored against predictions that were recorded in advance, not re-derived after
the fact.
"""

from __future__ import annotations

import json
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


@dataclass
class Ledger:
    path: Path = LEDGER_DB

    def __post_init__(self) -> None:
        ensure_dirs()
        with self.connect() as con:
            con.execute(SCHEMA)

    def connect(self) -> duckdb.DuckDBPyConnection:
        return duckdb.connect(str(self.path))

    def record(self, rows: pd.DataFrame) -> int:
        """Insert predictions. `features` is stored as JSON for later audit."""
        if rows.empty:
            return 0
        frame = rows.copy()
        if "features" in frame:
            frame["features"] = frame["features"].apply(
                lambda value: value if isinstance(value, str) else json.dumps(value)
            )
        with self.connect() as con:
            con.register("incoming", frame)
            con.execute(
                "INSERT OR IGNORE INTO predictions SELECT "
                + ", ".join(self._columns(con, "predictions", frame))
                + " FROM incoming"
            )
        return len(frame)

    def attach_outcome(self, rows: pd.DataFrame) -> int:
        if rows.empty:
            return 0
        with self.connect() as con:
            con.register("incoming_outcomes", rows)
            con.execute(
                "INSERT OR REPLACE INTO outcomes SELECT "
                + ", ".join(self._columns(con, "outcomes", rows))
                + " FROM incoming_outcomes"
            )
        return len(rows)

    def pending(self, now: pd.Timestamp) -> pd.DataFrame:
        """Predictions whose horizon has elapsed but which have no outcome yet."""
        with self.connect() as con:
            return con.execute(
                """
                SELECT p.*
                FROM predictions p
                LEFT JOIN outcomes o USING (prediction_id)
                WHERE o.prediction_id IS NULL
                  AND p.bar_timestamp + INTERVAL 1 MINUTE * p.horizon_minutes <= ?
                ORDER BY p.bar_timestamp
                """,
                [now],
            ).fetch_df()

    def scored(self) -> pd.DataFrame:
        with self.connect() as con:
            return con.execute(
                "SELECT * FROM predictions JOIN outcomes USING (prediction_id)"
            ).fetch_df()

    @staticmethod
    def _columns(con: duckdb.DuckDBPyConnection, table: str, frame: pd.DataFrame) -> list[str]:
        """Align incoming columns to the table order, NULL for anything missing."""
        table_columns = [row[0] for row in con.execute(f"DESCRIBE {table}").fetchall()]
        return [c if c in frame.columns else "NULL" for c in table_columns]
