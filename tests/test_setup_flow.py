"""Credential loading, the bootstrap state machine, and the setup redirect."""

from __future__ import annotations

import json
import stat

import pytest
from fastapi.testclient import TestClient

from intraday import bootstrap as bootstrap_module
from intraday import credentials as credentials_module
from intraday.bootstrap import (
    BootstrapRunner,
    BootstrapState,
    BootstrapSteps,
    describe,
    load_state,
    reconcile,
    save_state,
)
from intraday.credentials import load_credentials, mask, save_credentials


@pytest.fixture(autouse=True)
def clean_environment(monkeypatch):
    monkeypatch.delenv("ALPACA_API_KEY_ID", raising=False)
    monkeypatch.delenv("ALPACA_SECRET_KEY", raising=False)


# -- credentials ----------------------------------------------------------


def test_missing_credentials_return_none(tmp_path):
    assert load_credentials(tmp_path / "credentials.json") is None


def test_saved_credentials_round_trip_and_are_private(tmp_path):
    path = tmp_path / "credentials.json"
    save_credentials("KEY123456", "SECRET987654", path)

    mode = stat.S_IMODE(path.stat().st_mode)
    assert mode == 0o600

    loaded = load_credentials(path)
    assert loaded is not None
    assert (loaded.key_id, loaded.secret_key, loaded.source) == (
        "KEY123456",
        "SECRET987654",
        "file",
    )


def test_environment_wins_over_the_file(tmp_path, monkeypatch):
    path = tmp_path / "credentials.json"
    save_credentials("FILEKEY", "FILESECRET", path)
    monkeypatch.setenv("ALPACA_API_KEY_ID", "ENVKEY")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "ENVSECRET")

    loaded = load_credentials(path)
    assert loaded is not None
    assert (loaded.key_id, loaded.secret_key, loaded.source) == (
        "ENVKEY",
        "ENVSECRET",
        "environment",
    )


def test_incomplete_or_corrupt_files_are_treated_as_missing(tmp_path):
    half = tmp_path / "half.json"
    half.write_text(json.dumps({"key_id": "ONLY"}))
    assert load_credentials(half) is None

    broken = tmp_path / "broken.json"
    broken.write_text("{not json")
    assert load_credentials(broken) is None


def test_mask_never_shows_the_whole_key():
    masked = mask("PKABCDEFGH1234")
    assert "CDEFGH12" not in masked
    assert masked.startswith("PKAB")


def test_config_reads_saved_credentials(tmp_path, monkeypatch):
    from intraday.config import AlpacaConfig

    path = tmp_path / "credentials.json"
    save_credentials("CFGKEY", "CFGSECRET", path)
    monkeypatch.setattr(credentials_module, "CREDENTIALS_FILE", path)

    config = AlpacaConfig.from_env()
    assert config.key_id == "CFGKEY"


def test_config_raises_a_helpful_error_without_credentials(tmp_path, monkeypatch):
    from intraday.config import AlpacaConfig

    monkeypatch.setattr(credentials_module, "CREDENTIALS_FILE", tmp_path / "none.json")
    with pytest.raises(RuntimeError, match="/setup"):
        AlpacaConfig.from_env()


# -- bootstrap state machine ----------------------------------------------


def fake_steps(
    calls: dict, fail_on: str | None = None, bundle: tuple[bool, str] = (True, "bundle ok")
) -> BootstrapSteps:
    def symbols() -> list[str]:
        return ["AAA", "BBB", "CCC"]

    def sync_symbol(symbol: str, years: float) -> None:
        if fail_on == "sync":
            raise RuntimeError("network down")
        calls.setdefault("synced", []).append((symbol, round(years, 4)))

    def fetch_models() -> tuple[bool, str]:
        calls["fetch_models"] = calls.get("fetch_models", 0) + 1
        return bundle

    def build_dataset() -> int:
        if fail_on == "dataset":
            raise RuntimeError("no bars")
        calls["dataset"] = calls.get("dataset", 0) + 1
        return 1234

    def train(progress) -> int:
        calls["train"] = calls.get("train", 0) + 1
        progress(1, 2, "target 1 of 2")
        progress(2, 2, "target 2 of 2")
        return 2

    return BootstrapSteps(
        symbols=symbols,
        sync_symbol=sync_symbol,
        build_dataset=build_dataset,
        train=train,
        fetch_models=fetch_models,
    )


def test_published_models_skip_local_training(tmp_path):
    """Recent bars first, three years behind them, and no hours-long retrain."""
    calls: dict = {}
    path = tmp_path / "bootstrap.json"
    status = BootstrapRunner(path=path, steps=fake_steps(calls)).start(background=False)

    assert status["status"] == "done"
    assert calls["fetch_models"] == 1
    windows = [years for _, years in calls["synced"]]
    assert windows == [round(5 / 365, 4)] * 3 + [3.0] * 3
    assert "dataset" not in calls and "train" not in calls

    state = load_state(path)
    assert state.completed_stages == ["models", "recent", "backfill"]
    assert state.skipped_stages == ["dataset", "training"]
    assert state.models_source == "downloaded"


def test_a_missing_bundle_falls_back_to_training_on_this_machine(tmp_path):
    calls: dict = {}
    path = tmp_path / "bootstrap.json"
    steps = fake_steps(calls, bundle=(False, "No pre-trained models are published yet."))
    status = BootstrapRunner(path=path, steps=steps).start(background=False)

    assert status["status"] == "done"
    assert calls["dataset"] == 1 and calls["train"] == 1
    assert load_state(path).models_source == "local"


def test_the_dashboard_is_usable_once_recent_bars_are_in(tmp_path):
    running = describe(
        BootstrapState(status="running", stage="backfill", completed_stages=["models", "recent"])
    )
    assert running["usable"] is True

    early = describe(BootstrapState(status="running", stage="recent", completed_stages=["models"]))
    assert early["usable"] is False
    assert "goes live" in early["usable_note"]


def test_finished_bootstrap_does_not_run_again(tmp_path):
    path = tmp_path / "bootstrap.json"
    calls: dict = {}
    BootstrapRunner(path=path, steps=fake_steps(calls)).start(background=False)

    second: dict = {}
    BootstrapRunner(path=path, steps=fake_steps(second)).start(background=False)
    assert second == {}


def test_bootstrap_resumes_from_the_first_unfinished_stage(tmp_path):
    path = tmp_path / "bootstrap.json"
    save_state(
        BootstrapState(
            status="interrupted",
            stage="backfill",
            completed_stages=["models", "recent"],
            models_source="downloaded",
        ),
        path,
    )

    calls: dict = {}
    status = BootstrapRunner(path=path, steps=fake_steps(calls)).start(background=False)

    assert status["status"] == "done"
    assert "fetch_models" not in calls  # already installed, not downloaded twice
    assert [years for _, years in calls["synced"]] == [3.0] * 3


def test_failure_is_recorded_and_stays_resumable(tmp_path):
    path = tmp_path / "bootstrap.json"
    steps = fake_steps({}, fail_on="sync", bundle=(False, "no bundle"))
    status = BootstrapRunner(path=path, steps=steps).start(background=False)

    assert status["status"] == "failed"
    assert "network down" in status["error"]
    assert status["resumable"] is True
    assert load_state(path).completed_stages == ["models"]


def test_a_run_whose_process_vanished_is_reported_as_interrupted():
    state = BootstrapState(
        status="running",
        stage="recent",
        updated_at="2020-01-01T00:00:00+00:00",
        pid=999999,
    )
    assert reconcile(state).status == "interrupted"


def test_progress_is_never_invented():
    fresh = describe(BootstrapState(status="running", stage="backfill", current=0, total=36))
    assert fresh["eta_human"] == "estimating…"
    assert fresh["percent"] == 0
    assert fresh["message"] == "Filling in three years of history: 0 of 36"


# -- setup page and health -------------------------------------------------


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(credentials_module, "CREDENTIALS_FILE", tmp_path / "credentials.json")
    monkeypatch.setattr(bootstrap_module, "_runner", BootstrapRunner(path=tmp_path / "boot.json"))
    from intraday.web import app as web_app

    web_app._credentials_rejected.clear()
    with TestClient(web_app.app) as test_client:
        yield test_client


def test_root_redirects_to_setup_without_credentials(client):
    response = client.get("/", follow_redirects=False)
    assert response.status_code == 307
    assert response.headers["location"] == "/setup"


def test_setup_page_is_served(client):
    body = client.get("/setup").text
    assert "Alpaca" in body
    assert 'type="password"' in body


def test_root_serves_the_dashboard_once_credentials_exist(client, monkeypatch):
    save_credentials("KEY", "SECRET", credentials_module.CREDENTIALS_FILE)
    response = client.get("/", follow_redirects=False)
    assert response.status_code == 200
    assert "Forecasts" in response.text


def test_saving_bad_keys_reports_the_failure_and_stores_nothing(client, monkeypatch):
    monkeypatch.setattr(
        "intraday.web.app.verify_credentials", lambda k, s: (False, "Alpaca rejected those keys.")
    )
    body = client.post("/api/credentials", json={"key_id": "x", "secret_key": "y"}).json()

    assert body["ok"] is False
    assert not credentials_module.CREDENTIALS_FILE.exists()


def test_saving_good_keys_stores_them_and_starts_the_bootstrap(client, monkeypatch):
    started: list[bool] = []
    monkeypatch.setattr("intraday.web.app.verify_credentials", lambda k, s: (True, "Keys work."))
    monkeypatch.setattr(
        bootstrap_module.runner(), "start", lambda *a, **k: started.append(True) or {}
    )

    body = client.post(
        "/api/credentials", json={"key_id": "PKTESTKEY", "secret_key": "supersecret"}
    ).json()

    assert body["ok"] is True
    assert "supersecret" not in json.dumps(body)
    assert started == [True]
    assert load_credentials(credentials_module.CREDENTIALS_FILE).key_id == "PKTESTKEY"


def test_healthz_answers_before_anything_is_configured(client):
    body = client.get("/healthz").json()
    assert body["status"] == "ok"
    assert body["credentials"] is False
    assert body["bootstrap_status"] == "not_started"


def test_system_view_explains_what_is_missing(client):
    body = client.get("/api/system").json()
    names = [item["name"] for item in body["items"]]
    assert names == [
        "Market hours",
        "Dashboard",
        "Alpaca keys",
        "Live loop",
        "Market data",
        "Predictions today",
    ]
    assert body["credentials_ok"] is False
    assert any("No Alpaca keys" in warning for warning in body["warnings"])
