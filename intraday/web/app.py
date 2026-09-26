"""Dashboard API.

Three pages' worth of data: what the system predicts right now, how accurate
it has actually been, and the editable watchlist. Everything is served from
the ledger and the validation report, so the dashboard cannot show a number
the models did not really produce.
"""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from intraday.config import MODEL_DIR, load_watchlist, save_watchlist, settings_from_watchlist
from intraday.data.store import BarStore
from intraday.live.ledger import Ledger

STATIC_DIR = Path(__file__).parent / "static"

app = FastAPI(title="Intraday prediction engine")


class WatchlistUpdate(BaseModel):
    symbols: list[str]


def _ledger() -> Ledger:
    return Ledger()


@app.get("/api/watchlist")
def get_watchlist() -> dict:
    settings = settings_from_watchlist()
    store = BarStore()
    coverage = store.coverage()
    covered = set(coverage["symbol"]) if not coverage.empty else set()
    return {
        "symbols": list(load_watchlist()),
        "support_symbols": list(settings.support_symbols()),
        "missing_data": sorted(set(settings.all_symbols()) - covered),
    }


@app.post("/api/watchlist")
def update_watchlist(update: WatchlistUpdate) -> dict:
    if not update.symbols:
        raise HTTPException(status_code=400, detail="watchlist cannot be empty")
    symbols = save_watchlist(update.symbols)
    return {"symbols": list(symbols), "note": "run the data sync to backfill new symbols"}


@app.get("/api/forecasts")
def forecasts(limit: int = 100) -> dict:
    """Latest recorded prediction per symbol and horizon."""
    ledger = _ledger()
    with ledger.connect() as con:
        frame = con.execute(
            """
            SELECT * FROM predictions
            QUALIFY ROW_NUMBER() OVER (
                PARTITION BY symbol, horizon_minutes ORDER BY bar_timestamp DESC
            ) = 1
            ORDER BY expected_value_pct DESC
            LIMIT ?
            """,
            [limit],
        ).fetch_df()
    return {"rows": json.loads(frame.to_json(orient="records", date_format="iso"))}


@app.get("/api/validation")
def validation() -> dict:
    """Out-of-sample report plus live accuracy measured from the ledger."""
    report_path = MODEL_DIR / "validation_report.json"
    report = json.loads(report_path.read_text()) if report_path.exists() else []

    scored = _ledger().scored()
    live: list[dict] = []
    calibration: list[dict] = []
    if not scored.empty and "target_before_stop" in scored:
        usable = scored.dropna(subset=["target_before_stop", "probability"])
        for (model, horizon), group in usable.groupby(["model_version", "horizon_minutes"]):
            live.append(
                {
                    "model_version": model,
                    "horizon_minutes": int(horizon),
                    "n": int(len(group)),
                    "predicted_rate": float(group["probability"].mean()),
                    "actual_rate": float(group["target_before_stop"].mean()),
                    "mean_realized_return": float(group["realized_return"].mean()),
                }
            )
        buckets = pd.cut(usable["probability"], bins=[0, 0.2, 0.4, 0.5, 0.6, 0.8, 1.0])
        for bucket, group in usable.groupby(buckets, observed=True):
            calibration.append(
                {
                    "bucket": str(bucket),
                    "n": int(len(group)),
                    "predicted": float(group["probability"].mean()),
                    "actual": float(group["target_before_stop"].mean()),
                }
            )
    return {"walk_forward": report, "live": live, "calibration": calibration}


@app.get("/api/coverage")
def coverage() -> dict:
    frame = BarStore().coverage()
    return {"rows": json.loads(frame.to_json(orient="records", date_format="iso"))}


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
