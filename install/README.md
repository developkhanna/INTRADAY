# Installing Intraday on your Mac

Five minutes of your attention, once. After that the dashboard is simply there
every time you open the laptop.

Built for an Apple Silicon Mac (M1/M2/M3) on macOS 13 or newer. The installer
stops with a clear message on an Intel Mac rather than half-working.

It never touches what you already have: your positions, risk settings,
watchlist and prediction history in `~/.intraday` are left alone.

## 1. Download

Download this `install` folder (or the whole project) to your Mac — the
Downloads folder is fine.

## 2. Open the installer the first time: right-click, not double-click

The file is not signed by Apple, so a plain double-click shows
*"Apple could not verify 'Install Intraday.command' is free of malware"*.
That is expected. To get past it:

1. **Right-click** (or Control-click) **`Install Intraday.command`**.
2. Choose **Open**.
3. In the box that appears, click **Open** again.

macOS remembers this, so you only do it once per file.

If macOS shows no **Open** button at all (Sequoia and later), open
 **System Settings → Privacy & Security**, scroll to the bottom, and click
**Open Anyway** next to the message about `Install Intraday.command`, then
double-click the file again.

## 3. Watch it run

A black Terminal window appears and prints what it is doing. It may ask for
your Mac login password once — only if Python has to be installed. Nothing is
displayed while you type a password; that is normal.

When it finishes, a browser tab opens at <http://localhost:8000>.

## 4. Paste your Alpaca keys

The page asks for two values from a free Alpaca account and tells you exactly
where to get them. Paste both, click **Check and save**. That is the last
question you will be asked.

The app already has its trained models (the installer downloaded and
checksum-verified them), so all it needs now is prices: it fetches the last few
trading days first, which takes a few minutes, and the dashboard is live from
that point. The full three years of history keeps downloading behind it.

A banner at the top shows the stage, the count and the time left. Closing the
laptop is fine — it resumes.

If the model download had failed, the banner says so plainly and the Mac trains
its own models instead. That takes hours, not minutes, and it never pretends
otherwise.

## Every day after that

- The dashboard starts automatically when you log in.
- Open <http://localhost:8000>. Bookmark it, or drag `Open Intraday.command`
  to your Dock and click it.
- The **System** tab tells you in plain English whether everything is running
  and how fresh the data is. If the Mac was asleep, the page says so instead of
  showing stale numbers as if they were live.
- Times are shown on both clocks: yours in Abu Dhabi and New York's. The US
  session is 17:30–00:00 your time.
- The **System** tab also has the sleep switch: *"Keep this Mac awake during
  market hours so predictions keep recording"*. It is on by default. Turning it
  off stops holding the Mac awake immediately.

## The files in this folder

| File | What it does |
|---|---|
| `Install Intraday.command` | Installs or updates everything. Safe to run again any time. |
| `Open Intraday.command` | Opens the dashboard, starting it first if needed. |
| `Uninstall Intraday.command` | Stops the background services. Asks before deleting your data. |
| `bin/run-api.sh`, `bin/run-live-loop.sh` | What macOS runs in the background. You never call these yourself. |

## Where things live

| Path | What |
|---|---|
| `~/.intraday/app` | The program |
| `~/.intraday/venv` | Its private Python |
| `~/.intraday/logs` | Logs, trimmed automatically at 5 MB (3 kept) |
| `~/.intraday/credentials.json` | Your Alpaca keys, readable only by you |
| `~/.intraday/models` | The trained models and their validation report |
| `~/.intraday/preferences.json` | The keep-awake switch |
| `~/.intraday/bars`, `ledger.duckdb`, `positions.json`, `risk.json` | Your data and track record — back this folder up |

## If something looks wrong

1. Open the **System** tab on the dashboard; it names the part that is unhappy.
2. Run `Install Intraday.command` again — it repairs rather than duplicates.
3. Still stuck: `~/.intraday/logs/dashboard.log` holds the details.

## What this will never do

It never places an order. It writes down what it expects and why; you decide
and you trade. Models that have not beaten their own baseline out of sample are
labelled UNVALIDATED and cannot produce a BUY — today, all of them are.
