# INTRADAY

An intraday US-equity **prediction** engine. It forecasts what a stock is likely
to do over the next 5–60 minutes, scores itself honestly out of sample, and only
then turns a forecast into a suggested action. It never places an order.

## What it does, in plain English

1. **Downloads history** — every 1-minute bar for your watchlist from Alpaca's
   SIP feed (the full consolidated tape, free, back to 2016).
2. **Builds features** using only information available at that minute: returns,
   VWAP distance, relative volume, RSI, ATR, opening-range position, an estimated
   spread, and strength relative to SPY / QQQ / the stock's sector ETF.
3. **Builds labels** from the minutes *after*: forward return, best-case move
   (MFE), worst-case move (MAE), and "did the target get hit before the stop".
4. **Validates walk-forward** — trained on past days, tested on later days it has
   never seen, with a purge gap so no label leaks across the boundary.
5. **Costs the trade** — spread, slippage and IBKR commission are subtracted
   before any expectancy is reported.
6. **Decides** — BUY / BUY ON TRIGGER / NO TRADE, each with a one-paragraph
   explanation in everyday language.
7. **Records every prediction** in a ledger before the outcome is known, then
   attaches the real outcome later. The accuracy page is scored from that ledger.

If a model has not beaten its own baseline out of sample, it is labelled
`UNVALIDATED` and **cannot** produce a BUY. That refusal is a feature.

## Setup

On a Mac, none of this is necessary: double-click `install/Install Intraday.command`
and paste the Alpaca keys into the page it opens. See [install/README.md](install/README.md).
The rest of this section is the manual path.

```bash
pip install -e ".[dev]"
export ALPACA_API_KEY_ID=...      # paper-account keys are enough; data only
export ALPACA_SECRET_KEY=...
```

## Pipeline

```bash
python scripts/sync_history.py --years 3     # download 1-minute bars
python scripts/build_dataset.py              # features + labels -> parquet
python scripts/train_models.py --fit-production
uvicorn intraday.web.app:app --port 8000     # dashboard
python scripts/live_loop.py                  # predictions during the session
```

Data lives outside the repo, in `~/.intraday` (override with `INTRADAY_DATA_DIR`).

Without environment variables the dashboard serves `/setup` instead of crashing:
keys pasted there are checked against Alpaca and stored in `~/.intraday/credentials.json`
(mode 0600), and a background bootstrap then runs the three pipeline steps above
by itself, resumably, with a progress banner on the dashboard.

## Layout

| Path | Purpose |
|---|---|
| `intraday/data/` | Alpaca client, parquet bar store, incremental sync |
| `intraday/features/engineer.py` | point-in-time features |
| `intraday/labels/builder.py` | forward returns, MFE/MAE, target-before-stop |
| `intraday/models/` | walk-forward splits, training, calibration, reports |
| `intraday/decide.py` | cost model and the prediction → action layer |
| `intraday/live/` | live loop, prediction ledger, outcome attachment |
| `intraday/web/` | dashboard API and UI |
| `intraday/credentials.py` | env-first credential loading, 0600 storage |
| `intraday/bootstrap.py` | resumable first-run state machine |
| `intraday/health.py` | plain-English system status and staleness |
| `install/` | macOS double-click installer and LaunchAgents |

## Honesty rules baked into the code

- No random train/test shuffling; time order is always respected.
- Labels are computed separately from features and joined on key, never derived
  from the same pass.
- `tests/test_leakage.py` rebuilds the features on truncated data and asserts
  every value at time T is unchanged — a feature that peeks at the future fails.
- Probabilities are isotonic-calibrated on a held-out slice of the training
  window, and calibration error is reported per fold.
- Expectancy is always after costs.

## Known limits

- Free Alpaca SIP data is embargoed for the last ~15 minutes, so live quotes come
  from IEX (or IBKR later); models are trained to tolerate that difference.
- Regular trading hours only (09:30–16:00 ET).
- Long side only for now.
