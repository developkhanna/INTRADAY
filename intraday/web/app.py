"""Dashboard API.

Three pages' worth of data: what the system predicts right now, how accurate
it has actually been, and the editable watchlist. Everything is served from
the ledger and the validation report, so the dashboard cannot show a number
the models did not really produce.

On a fresh machine there are no credentials and no data, so `/` sends the
owner to `/setup`, and saving working keys there starts the bootstrap job that
downloads history, builds the dataset and trains the models in the background.
"""

from __future__ import annotations

import datetime as dt
import json
import threading
from contextlib import asynccontextmanager
from dataclasses import asdict, replace
from pathlib import Path

import pandas as pd
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from intraday import health
from intraday.bootstrap import runner as bootstrap_runner
from intraday.config import MODEL_DIR, load_watchlist, save_watchlist, settings_from_watchlist
from intraday.credentials import load_credentials, mask, save_credentials, verify_credentials
from intraday.data.store import BarStore
from intraday.live.ledger import Ledger
from intraday.positions import Position, load_positions, review, save_positions
from intraday.risk import load_risk, save_risk

STATIC_DIR = Path(__file__).parent / "static"

# Set when Alpaca tells us the stored keys no longer work, so `/` can send the
# owner back to the setup page instead of showing a dashboard that cannot load.
_credentials_rejected: dict[str, str] = {}


class WatchlistUpdate(BaseModel):
    symbols: list[str]


class CredentialsInput(BaseModel):
    key_id: str
    secret_key: str


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


def _check_credentials_and_resume() -> None:
    """Resume setup by itself: the owner never re-runs anything by hand."""
    credentials = load_credentials()
    if credentials is None:
        return
    ok, message = verify_credentials(credentials.key_id, credentials.secret_key)
    if not ok:
        _credentials_rejected["message"] = message
        return
    _credentials_rejected.clear()
    if bootstrap_runner().state().status != "done":
        bootstrap_runner().start()


@asynccontextmanager
async def lifespan(_: FastAPI):
    # In a thread: a slow Alpaca call must not delay the page the owner is
    # already staring at.
    threading.Thread(
        target=_check_credentials_and_resume, name="intraday-startup", daemon=True
    ).start()
    yield


app = FastAPI(title="Intraday prediction engine", lifespan=lifespan)


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


# -- first-run setup ------------------------------------------------------


def credentials_ready() -> bool:
    """Keys exist and have not been rejected by Alpaca since the app started."""
    return load_credentials() is not None and not _credentials_rejected


@app.get("/setup")
def setup_page() -> FileResponse:
    return FileResponse(STATIC_DIR / "setup.html")


@app.get("/api/credentials")
def credentials_status() -> dict:
    credentials = load_credentials()
    return {
        "configured": credentials is not None,
        "source": credentials.source if credentials else None,
        "key_id_masked": mask(credentials.key_id) if credentials else None,
        "rejected": _credentials_rejected.get("message", ""),
    }


@app.post("/api/credentials")
def save_credentials_endpoint(payload: CredentialsInput) -> dict:
    """Check the keys with Alpaca, then store them 0600. Never echoed back."""
    key_id = payload.key_id.strip()
    secret_key = payload.secret_key.strip()
    if not key_id or not secret_key:
        raise HTTPException(status_code=400, detail="Both the key ID and the secret are needed.")

    ok, message = verify_credentials(key_id, secret_key)
    if not ok:
        return {"ok": False, "message": message}

    save_credentials(key_id, secret_key)
    _credentials_rejected.clear()
    bootstrap_runner().start()
    return {
        "ok": True,
        "message": message,
        "key_id_masked": mask(key_id),
        "next": "Setup has started downloading market data in the background.",
    }


@app.get("/api/bootstrap")
def bootstrap_status() -> dict:
    return bootstrap_runner().status()


@app.post("/api/bootstrap")
def bootstrap_start() -> dict:
    if not credentials_ready():
        raise HTTPException(status_code=400, detail="Save your Alpaca keys first.")
    return bootstrap_runner().start()


def _last_bar_timestamp() -> dt.datetime | None:
    coverage = BarStore().coverage()
    if coverage.empty or "last" not in coverage:
        return None
    return pd.Timestamp(coverage["last"].max()).to_pydatetime()


def _predictions_today() -> int:
    midnight = dt.datetime.now(dt.timezone.utc).replace(
        hour=0, minute=0, second=0, microsecond=0, tzinfo=None
    )
    with _ledger().connect() as con:
        return int(
            con.execute(
                "SELECT COUNT(*) FROM predictions WHERE created_at >= ?", [midnight]
            ).fetchone()[0]
        )


@app.get("/api/system")
def system() -> dict:
    snapshot = health.snapshot(
        last_bar=_last_bar_timestamp(), predictions_today=_predictions_today()
    )
    snapshot["bootstrap"] = bootstrap_runner().status()
    return snapshot


@app.get("/healthz")
def healthz() -> dict:
    """Small enough to poll: is the app up, and does it have what it needs."""
    state = bootstrap_runner().state()
    return {
        "status": "ok",
        "credentials": credentials_ready(),
        "bootstrap_stage": state.stage,
        "bootstrap_status": state.status,
        "time": dt.datetime.now(dt.timezone.utc).isoformat(),
    }


@app.get("/api/coverage")
def coverage() -> dict:
    frame = BarStore().coverage()
    return {"rows": json.loads(frame.to_json(orient="records", date_format="iso"))}


@app.get("/", response_model=None)
def index() -> FileResponse | RedirectResponse:
    if not credentials_ready():
        return RedirectResponse(url="/setup", status_code=307)
    return FileResponse(STATIC_DIR / "index.html")


if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
