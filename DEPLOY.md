# Deployment plan

## What gets deployed

One small Python service, three processes:

| Process | What it does | Runs when |
|---|---|---|
| `uvicorn intraday.web.app:app` | The dashboard + JSON API you open in a browser | always |
| `scripts/live_loop.py` | Every minute: pulls fresh bars, scores the models, writes each prediction to the ledger *before* the outcome is known, attaches outcomes later | 09:30-16:00 New York |
| `scripts/sync_history.py` (cron, nightly) | Appends the day's bars to the local store so the next retrain sees them | after the close |

State lives in `~/.intraday` (bars as Parquet, `ledger.duckdb`, `positions.json`, `risk.json`, `watchlist.json`). No database server to run. Back this folder up; it holds the whole prediction track record.

Resource shape: ~2 vCPU / 4 GB RAM / 40 GB disk is comfortable. The 3-year bar store is ~500 MB today and grows ~1 MB per symbol per month.

## Where it can run

| Option | Cost | Notes |
|---|---|---|
| **A. Small cloud VM (recommended)** | $6-12/mo | Hetzner / DigitalOcean / Lightsail. Always on, so the live loop records predictions while you are off-screen. You open the dashboard from laptop or phone. |
| **B. Your own machine** | free | Only records predictions while the machine is awake. Given your hours (17:30-00:00 local) and your wish to step away, this loses data on exactly the evenings you are out. |
| **C. Managed platform (Fly.io / Render)** | ~$7/mo | Simplest ops, but needs a persistent volume for `~/.intraday`, and free tiers sleep the container, which silently kills the live loop. |

I recommend **A**.

## What I need from you

1. **Pick A, B, or C.** If A: create the VM in your own account and give me SSH access (or run three commands I hand you). I cannot deploy to the public internet from my side on your behalf — that is a hard policy limit here, so the hosting account has to be yours.
2. **A domain, or not.** Without one you reach it at `http://<server-ip>:8000`. With one I put Caddy in front for HTTPS, which you want if you are logging in from your phone over hotel/office wifi.
3. **A password.** The dashboard has no auth today. Anything internet-facing gets HTTP basic auth at minimum before it goes up. Pick a password and I will store it as a secret, never in code.

Nothing else. The Alpaca keys are already stored as session secrets and get injected as environment variables on the server.

## Steps once you pick

1. Harden: basic auth on every route, bind to localhost, Caddy reverse proxy with automatic TLS.
2. Package: `systemd` units for the API and the live loop, with restart-on-failure and a market-hours timer; nightly cron for the sync.
3. Ship: clone the repo on the server, `pip install -e .`, copy `~/.intraday` (or re-sync history from Alpaca, ~40 minutes).
4. Verify: dashboard loads over HTTPS, live loop writes a prediction in the ledger, restart the box and confirm both come back on their own.
5. Watch: a `/healthz` endpoint plus a nightly digest in the dashboard showing bars ingested, predictions recorded, outcomes attached.

Budget: one session for steps 1-2, one short session for 3-5 once the server exists.

## The honest blocker

The dashboard is ready to deploy. **The models are not ready to trade.** Out-of-sample AUC is ~0.52-0.53 across horizons, which is a real but weak signal, so every model is flagged `UNVALIDATED` and the engine refuses to emit a BUY. Deploying now is still worth it, for one specific reason: the live loop starts recording predictions before outcomes are known, which is the only honest way to find out whether the edge is real. Expect to run it in record-only mode for a few weeks before any money follows it.
