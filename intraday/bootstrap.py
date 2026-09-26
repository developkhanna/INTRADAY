"""First-run bootstrap: download history, build the dataset, train the models.

The dashboard runs this in a background thread the first time credentials
exist, so a non-technical owner never has to type a command. It is a small
state machine persisted to `~/.intraday/bootstrap.json`:

    history -> dataset -> training -> done

Every stage is resumable. Closing the laptop mid-run leaves a stale "running"
state, which is reconciled to "interrupted" on the next start and picked up
from the first unfinished stage. `sync_history` already resumes per symbol, so
no downloaded bar is ever fetched twice.

Nothing here invents progress: counters come from work actually completed, and
the ETA stays "estimating" until at least one unit of work has finished.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import os
import threading
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path

from intraday.config import DATA_DIR, DATASET_DIR, MODEL_DIR, ensure_dirs, settings_from_watchlist

logger = logging.getLogger(__name__)

STATE_FILE = DATA_DIR / "bootstrap.json"

STAGES = ("history", "dataset", "training")

STAGE_LABELS = {
    "history": "Downloading market history",
    "dataset": "Building the training table",
    "training": "Training models",
    "done": "Ready",
}

# A heartbeat older than this means the process that owned the run is gone
# (laptop closed, crash, restart).
HEARTBEAT_TIMEOUT_SECONDS = 300


@dataclass
class BootstrapState:
    status: str = "not_started"  # not_started | running | done | failed | interrupted
    stage: str = "history"
    current: int = 0
    total: int = 0
    detail: str = ""
    error: str = ""
    completed_stages: list[str] = field(default_factory=list)
    started_at: str = ""
    updated_at: str = ""
    stage_started_at: str = ""
    pid: int = 0

    def to_dict(self) -> dict:
        return asdict(self)


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def _age_seconds(timestamp: str) -> float | None:
    if not timestamp:
        return None
    try:
        moment = dt.datetime.fromisoformat(timestamp)
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=dt.timezone.utc)
    return (dt.datetime.now(dt.timezone.utc) - moment).total_seconds()


def load_state(path: Path | None = None) -> BootstrapState:
    path = path or STATE_FILE
    if not path.exists():
        return BootstrapState()
    try:
        payload = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return BootstrapState()
    known = {f for f in BootstrapState().to_dict()}
    return BootstrapState(**{k: v for k, v in payload.items() if k in known})


def save_state(state: BootstrapState, path: Path | None = None) -> None:
    path = path or STATE_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    state.updated_at = _now()
    temp = path.with_suffix(".json.tmp")
    temp.write_text(json.dumps(state.to_dict(), indent=2))
    temp.replace(path)


def reconcile(state: BootstrapState) -> BootstrapState:
    """A run whose owning process stopped heart-beating was interrupted."""
    if state.status != "running":
        return state
    stale = (_age_seconds(state.updated_at) or 0) > HEARTBEAT_TIMEOUT_SECONDS
    if stale or (state.pid and state.pid != os.getpid() and not _process_alive(state.pid)):
        state.status = "interrupted"
        state.detail = "Interrupted (the laptop slept or the app restarted). Safe to resume."
    return state


def _process_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def estimate_remaining_seconds(state: BootstrapState) -> float | None:
    """Seconds left in the current stage, or None while there is nothing to go on."""
    if state.status != "running" or state.current <= 0 or state.total <= 0:
        return None
    elapsed = _age_seconds(state.stage_started_at)
    if elapsed is None or elapsed <= 0:
        return None
    per_unit = elapsed / state.current
    return max(0.0, per_unit * (state.total - state.current))


def humanize_seconds(seconds: float | None) -> str:
    if seconds is None:
        return "estimating…"
    if seconds < 90:
        return "less than 2 minutes"
    minutes = int(round(seconds / 60))
    if minutes < 60:
        return f"about {minutes} minutes"
    hours = minutes / 60
    return f"about {hours:.1f} hours"


def describe(state: BootstrapState) -> dict:
    """Everything the dashboard banner needs, in plain English."""
    remaining = estimate_remaining_seconds(state)
    label = STAGE_LABELS.get(state.stage, state.stage)
    if state.status == "done":
        message = "Setup finished. The engine has history, a dataset and trained models."
    elif state.status == "not_started":
        message = "First-time setup has not started yet."
    elif state.status == "failed":
        message = f"Setup stopped during “{label}”. {state.error}"
    elif state.status == "interrupted":
        message = f"Setup was interrupted during “{label}”. It can pick up where it left off."
    elif state.total:
        message = f"{label}: {state.current} of {state.total}"
    else:
        message = f"{label}…"
    return {
        **state.to_dict(),
        "stage_label": label,
        "message": message,
        "detail": state.detail,
        "eta_human": humanize_seconds(remaining),
        "eta_seconds": remaining,
        "percent": round(100 * state.current / state.total) if state.total else None,
        "stages_total": len(STAGES),
        "stage_number": STAGES.index(state.stage) + 1 if state.stage in STAGES else len(STAGES),
    }


ProgressHook = Callable[[int, int, str], None]


@dataclass
class BootstrapSteps:
    """The three real units of work, injectable so tests need no market data."""

    symbols: Callable[[], list[str]]
    sync_symbol: Callable[[str], None]
    build_dataset: Callable[[], int]
    train: Callable[[ProgressHook], int]


def default_steps(years: float = 3.0) -> BootstrapSteps:
    def symbols() -> list[str]:
        return list(settings_from_watchlist().all_symbols())

    def sync_symbol(symbol: str) -> None:
        from intraday.data.sync import sync_history

        sync_history(settings=settings_from_watchlist(), years=years, symbols=[symbol])

    def build() -> int:
        from intraday.dataset import build_dataset

        return len(build_dataset(settings=settings_from_watchlist()))

    def train(progress: ProgressHook) -> int:
        import pandas as pd

        from intraday.models.train import evaluate_target, train_production_model, write_report

        dataset = pd.read_parquet(DATASET_DIR / "dataset.parquet")
        targets = [c for c in dataset.columns if c.startswith("tbs_")]
        reports = []
        for index, target in enumerate(targets, start=1):
            progress(index - 1, len(targets), f"target {index} of {len(targets)}: {target}")
            report = evaluate_target(dataset, target)
            reports.append(report.to_dict())
            # A model that has not beaten its baseline out of sample is never
            # refit for live use; the decision layer refuses to buy on it.
            if report.status == "VALIDATED":
                train_production_model(dataset, target)
            progress(index, len(targets), f"target {index} of {len(targets)}: {target}")
        write_report(reports)
        return len(reports)

    return BootstrapSteps(
        symbols=symbols, sync_symbol=sync_symbol, build_dataset=build, train=train
    )


class BootstrapRunner:
    """Owns the background thread and the persisted state."""

    def __init__(self, path: Path | None = None, steps: BootstrapSteps | None = None):
        self.path = path or STATE_FILE
        self._steps = steps
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()

    @property
    def steps(self) -> BootstrapSteps:
        if self._steps is None:
            self._steps = default_steps()
        return self._steps

    def state(self) -> BootstrapState:
        state = reconcile(load_state(self.path))
        return state

    def status(self) -> dict:
        state = self.state()
        payload = describe(state)
        payload["running"] = bool(self._thread and self._thread.is_alive())
        payload["resumable"] = state.status in ("interrupted", "failed", "not_started")
        return payload

    def start(self, background: bool = True) -> dict:
        """Start (or resume) the bootstrap. Safe to call repeatedly."""
        with self._lock:
            if self._thread and self._thread.is_alive():
                return self.status()
            state = self.state()
            if state.status == "done":
                return self.status()
            if background:
                self._thread = threading.Thread(
                    target=self._run_guarded, name="intraday-bootstrap", daemon=True
                )
                self._thread.start()
            else:
                self._run_guarded()
            return self.status()

    def reset(self) -> None:
        save_state(BootstrapState(), self.path)

    # -- internals ---------------------------------------------------------

    def _write(self, state: BootstrapState) -> None:
        save_state(state, self.path)

    def _run_guarded(self) -> None:
        try:
            self._run()
        except Exception as exc:  # the thread must never take the API down
            logger.exception("bootstrap failed")
            state = load_state(self.path)
            state.status = "failed"
            state.error = f"{type(exc).__name__}: {exc}"
            self._write(state)

    def _run(self) -> None:
        ensure_dirs()
        state = self.state()
        if state.status == "done":
            return
        state.status = "running"
        state.error = ""
        state.pid = os.getpid()
        state.started_at = state.started_at or _now()

        for stage in STAGES:
            if stage in state.completed_stages:
                continue
            state.stage = stage
            state.current = 0
            state.total = 0
            state.detail = ""
            state.stage_started_at = _now()
            self._write(state)
            getattr(self, f"_stage_{stage}")(state)
            state.completed_stages.append(stage)
            self._write(state)

        state.stage = "done"
        state.status = "done"
        state.current = state.total = 0
        state.detail = ""
        self._write(state)

    def _stage_history(self, state: BootstrapState) -> None:
        symbols = self.steps.symbols()
        state.total = len(symbols)
        state.detail = "Three years of 1-minute bars. The first run takes a while."
        self._write(state)
        for index, symbol in enumerate(symbols, start=1):
            self.steps.sync_symbol(symbol)
            state.current = index
            state.detail = f"{symbol} done"
            self._write(state)

    def _stage_dataset(self, state: BootstrapState) -> None:
        state.total = 1
        state.detail = "Turning bars into point-in-time features and forward labels."
        self._write(state)
        rows = self.steps.build_dataset()
        state.current = 1
        state.detail = f"{rows:,} rows built"
        self._write(state)

    def _stage_training(self, state: BootstrapState) -> None:
        def progress(current: int, total: int, detail: str) -> None:
            state.current, state.total, state.detail = current, total, detail
            self._write(state)

        state.detail = "Walk-forward validation on days the model has never seen."
        self._write(state)
        self.steps.train(progress)


_runner: BootstrapRunner | None = None


def runner() -> BootstrapRunner:
    """Process-wide runner, so the API and the banner see the same run."""
    global _runner
    if _runner is None:
        _runner = BootstrapRunner()
    return _runner


def models_ready() -> bool:
    return (MODEL_DIR / "validation_report.json").exists()


def wait_until_idle(run: BootstrapRunner, timeout: float = 30.0) -> dict:
    """Test helper: block until the background thread has finished."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not (run._thread and run._thread.is_alive()):
            return run.status()
        time.sleep(0.02)
    raise TimeoutError("bootstrap did not finish in time")
