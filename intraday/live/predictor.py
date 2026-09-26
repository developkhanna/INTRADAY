"""Live prediction loop.

Once a minute during the US session: pull the latest bars, rebuild features
point-in-time, ask each model, turn probabilities into actions through the
cost model, and write everything to the ledger before the outcome is known.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import uuid

import pandas as pd

from intraday.config import MODEL_DIR, Settings
from intraday.data.alpaca import AlpacaClient, AlpacaConfig
from intraday.data.store import BarStore
from intraday.decide import CostModel, decide
from intraday.features.engineer import build_features
from intraday.live.ledger import Ledger

logger = logging.getLogger(__name__)

WARMUP_DAYS = 5


def load_models(targets: list[str]) -> dict[str, dict]:
    import joblib

    models = {}
    for target in targets:
        path = MODEL_DIR / f"{target}.joblib"
        if path.exists():
            models[target] = joblib.load(path)
        else:
            logger.warning("no trained model for %s", target)
    return models


def _model_statuses() -> dict[str, str]:
    path = MODEL_DIR / "validation_report.json"
    if not path.exists():
        return {}
    return {r["target"]: r.get("status", "UNVALIDATED") for r in json.loads(path.read_text())}


def run_once(
    targets: list[str],
    settings: Settings | None = None,
    client: AlpacaClient | None = None,
    store: BarStore | None = None,
    ledger: Ledger | None = None,
    cost_model: CostModel | None = None,
    account_equity: float = 25_000.0,
) -> pd.DataFrame:
    """Fetch, predict and record one minute's worth of decisions."""
    settings = settings or Settings()
    client = client or AlpacaClient(AlpacaConfig.from_env())
    store = store or BarStore()
    ledger = ledger or Ledger()
    cost_model = cost_model or CostModel()

    now = pd.Timestamp.utcnow().tz_localize("UTC")
    start = (now - dt.timedelta(days=WARMUP_DAYS)).to_pydatetime()
    fresh = client.bars(
        list(settings.all_symbols()),
        start,
        now.to_pydatetime(),
        feed=client.config.realtime_feed,
    )
    if not fresh.empty:
        store.write(fresh)

    bars = store.read(list(settings.all_symbols()), start=start.date())
    features = build_features(bars, settings=settings)
    if features.empty:
        return pd.DataFrame()

    latest = features.sort_values("timestamp").groupby("symbol").tail(1)
    quote_frame = client.latest_quotes(list(settings.universe))
    quotes = (
        quote_frame.set_index("symbol").to_dict("index") if not quote_frame.empty else {}
    )
    statuses = _model_statuses()
    models = load_models(targets)

    records = []
    for target, bundle in models.items():
        horizon = int(target.split("_")[-1].removesuffix("m"))
        columns = bundle["features"]
        present = [c for c in columns if c in latest.columns]
        usable = latest.dropna(subset=present, thresh=int(0.8 * len(columns)))
        if usable.empty:
            continue
        raw = bundle["model"].predict_proba(usable[columns])[:, 1]
        probability = bundle["calibrator"].predict(raw)

        for (_, row), p in zip(usable.iterrows(), probability, strict=True):
            quote = quotes.get(row["symbol"], {})
            price = float(row["close_price"])
            spread_pct = (
                (quote["ask"] - quote["bid"]) / price
                if quote.get("ask") and quote.get("bid")
                else float(row.get("spread_est") or 0.0005)
            )
            atr_pct = float(row.get("atr_pct") or 0.002)
            target_pct, stop_pct = atr_pct * 1.0, atr_pct * 0.5
            cost_pct = cost_model.round_trip(price, spread_pct, shares=200)

            idea = decide(
                symbol=row["symbol"],
                horizon_minutes=horizon,
                probability=float(p),
                target_pct=target_pct,
                stop_pct=stop_pct,
                cost_pct=cost_pct,
                model_status=statuses.get(target, "UNVALIDATED"),
                liquidity_ok=spread_pct < 0.002,
            )
            records.append(
                {
                    "prediction_id": str(uuid.uuid4()),
                    "created_at": now,
                    "bar_timestamp": row["timestamp"],
                    "symbol": row["symbol"],
                    "horizon_minutes": horizon,
                    "model_version": target,
                    "model_status": idea.model_status,
                    "probability": idea.probability,
                    "target_pct": idea.target_pct,
                    "stop_pct": idea.stop_pct,
                    "cost_pct": idea.cost_pct,
                    "expected_value_pct": idea.expected_value_pct,
                    "action": idea.action,
                    "reason": idea.reason,
                    "eli5": idea.eli5,
                    "price_at_prediction": price,
                    "features": json.dumps(
                        {c: _plain(row[c]) for c in columns if c in row.index}
                    ),
                }
            )

    frame = pd.DataFrame(records)
    ledger.record(frame)
    return frame


def _plain(value: object) -> object:
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return str(value)
    return None if pd.isna(number) else round(number, 6)
