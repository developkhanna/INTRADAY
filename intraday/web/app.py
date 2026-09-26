"""Dashboard API.

Three pages' worth of data: what the system predicts right now, how accurate
it has actually been, and the editable watchlist. Everything is served from
the ledger and the validation report, so the dashboard cannot show a number
the models did not really produce.
"""

from __future__ import annotations

import json
from dataclasses import asdict, replace
from pathlib import Path

import pandas as pd
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from intraday.config import MODEL_DIR, load_watchlist, save_watchlist, settings_from_watchlist
from intraday.data.store import BarStore
from intraday.live.ledger import Ledger
from intraday.positions import Position, load_positions, review, save_positions
from intraday.risk import load_risk, save_risk

STATIC_DIR = Path(__file__).parent / "static"

app = FastAPI(title="Intraday prediction engine")


class WatchlistUpdate(BaseModel):
    symbols: list[str]


class PositionInput(BaseModel):
    symbol: str
    quantity: float
    avg_price: float


class PositionsUpdate(BaseModel):
    positions: list[PositionInput]


class RiskUpdate(BaseModel):
    account_equity: float
    position_size: float
    hard_stop_pct: float
    daily_loss_limit_pct: float
    max_open_positions: int
    honor_stop_on_model_trades: bool


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


@app.get("/api/risk")
def get_risk() -> dict:
    profile = load_risk()
    return {
        "risk": asdict(profile),
        "hard_stop_dollars": profile.hard_stop_dollars,
        "daily_loss_limit_dollars": profile.daily_loss_limit_dollars,
    }


@app.post("/api/risk")
def update_risk(update: RiskUpdate) -> dict:
    current = load_risk()
    profile = replace(current, **update.model_dump())
    if not 0 < profile.hard_stop_pct <= 1:
        raise HTTPException(status_code=400, detail="hard_stop_pct must be between 0 and 1")
    save_risk(profile)
    return {"risk": asdict(profile)}


def _latest_prices(symbols: list[str]) -> dict[str, float]:
    if not symbols:
        return {}
    store = BarStore()
    prices: dict[str, float] = {}
    for symbol in symbols:
        bars = store.read([symbol])
        if not bars.empty:
            prices[symbol] = float(bars["close"].iloc[-1])
    return prices


def _latest_views(horizon_minutes: int) -> dict[str, tuple[float, str]]:
    """Most recent recorded probability and model status per symbol."""
    with _ledger().connect() as con:
        frame = con.execute(
            """
            SELECT symbol, probability, model_status FROM predictions
            WHERE horizon_minutes = ?
            QUALIFY ROW_NUMBER() OVER (PARTITION BY symbol ORDER BY bar_timestamp DESC) = 1
            """,
            [horizon_minutes],
        ).fetch_df()
    return {
        str(row.symbol): (float(row.probability), str(row.model_status))
        for row in frame.itertuples()
    }


@app.get("/api/positions")
def get_positions(horizon_minutes: int = 30) -> dict:
    positions = load_positions()
    risk = load_risk()
    prices = _latest_prices([p.symbol for p in positions])
    views = _latest_views(horizon_minutes)
    rows = []
    for position in positions:
        price = prices.get(position.symbol, position.avg_price)
        probability, model_status = views.get(position.symbol, (None, "UNVALIDATED"))
        rows.append(asdict(review(position, price, risk, probability, model_status)))
    rows.sort(key=lambda r: (-r["urgency"], r["unrealized_pct"]))
    return {
        "rows": rows,
        "attention": [r for r in rows if r["urgency"] >= 2],
        "priced_from": "last stored 1-minute bar",
        "horizon_minutes": horizon_minutes,
    }


@app.post("/api/positions")
def update_positions(update: PositionsUpdate) -> dict:
    positions = [
        Position(symbol=item.symbol, quantity=item.quantity, avg_price=item.avg_price)
        for item in update.positions
    ]
    save_positions(positions)
    return {"positions": [asdict(p) for p in positions]}


@app.get("/api/coverage")
def coverage() -> dict:
    frame = BarStore().coverage()
    return {"rows": json.loads(frame.to_json(orient="records", date_format="iso"))}


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
