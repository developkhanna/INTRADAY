"""Pre-trained model delivery and the keep-awake switch."""

from __future__ import annotations

import hashlib
import io
import json
import tarfile

import pytest

from intraday import awake as awake_module
from intraday import modelbundle
from intraday.awake import AwakeKeeper
from intraday.modelbundle import download_bundle

# -- model bundle ---------------------------------------------------------


def make_archive(names: list[str]) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for name in names:
            payload = name.encode()
            info = tarfile.TarInfo(name)
            info.size = len(payload)
            archive.addfile(info, io.BytesIO(payload))
    return buffer.getvalue()


@pytest.fixture
def serve(monkeypatch, tmp_path):
    """Stand in for the release download, without touching the network."""

    def install(archive: bytes | None, checksum: str | None):
        def fetch(url: str) -> bytes:
            if url.endswith(".sha256"):
                if checksum is None:
                    raise OSError("404")
                return f"{checksum}  models.tar.gz\n".encode()
            if archive is None:
                raise OSError("404")
            return archive

        monkeypatch.setattr(modelbundle, "_fetch", fetch)
        monkeypatch.setattr(modelbundle, "MARKER", tmp_path / "bundle.json")

    return install


def test_a_verified_bundle_is_unpacked_into_the_models_folder(serve, tmp_path):
    archive = make_archive(["tbs_t1_s0p5_30m.joblib", "validation_report.json"])
    serve(archive, hashlib.sha256(archive).hexdigest())

    destination = tmp_path / "models"
    result = download_bundle("https://example/models.tar.gz", destination=destination)

    assert result.ok is True
    assert result.files == 2
    assert sorted(p.name for p in destination.iterdir()) == [
        "tbs_t1_s0p5_30m.joblib",
        "validation_report.json",
    ]
    assert json.loads(modelbundle.MARKER.read_text())["sha256"] == result.sha256


def test_a_wrong_checksum_installs_nothing(serve, tmp_path):
    archive = make_archive(["tbs_t1_s0p5_30m.joblib"])
    serve(archive, "0" * 64)

    destination = tmp_path / "models"
    result = download_bundle("https://example/models.tar.gz", destination=destination)

    assert result.ok is False
    assert "checksum" in result.message
    assert list(destination.iterdir()) == []


def test_a_missing_release_asset_is_reported_not_raised(serve, tmp_path):
    serve(None, None)

    result = download_bundle("https://example/models.tar.gz", destination=tmp_path / "models")

    assert result.ok is False
    assert "Training locally instead." in result.message


def test_an_archive_that_tries_to_escape_is_rejected(serve, tmp_path):
    archive = make_archive(["../escaped.joblib"])
    serve(archive, hashlib.sha256(archive).hexdigest())

    destination = tmp_path / "models"
    result = download_bundle("https://example/models.tar.gz", destination=destination)

    assert result.ok is False
    assert not (tmp_path / "escaped.joblib").exists()


# -- keep awake -----------------------------------------------------------


@pytest.fixture
def prefs(monkeypatch, tmp_path):
    monkeypatch.setattr(awake_module, "PREFS_FILE", tmp_path / "preferences.json")


def test_keep_awake_defaults_to_on(prefs):
    assert awake_module.keep_awake_enabled() is True


def test_caffeinate_runs_only_while_the_market_is_open(prefs, monkeypatch):
    keeper = AwakeKeeper(command=["sleep", "60"])
    monkeypatch.setattr(awake_module, "supported", lambda: True)

    monkeypatch.setattr(awake_module, "market_is_open", lambda now=None: True)
    assert keeper.reconcile() is True

    monkeypatch.setattr(awake_module, "market_is_open", lambda now=None: False)
    assert keeper.reconcile() is False


def test_turning_the_switch_off_stops_the_process_immediately(prefs, monkeypatch):
    keeper = AwakeKeeper(command=["sleep", "60"])
    monkeypatch.setattr(awake_module, "supported", lambda: True)
    monkeypatch.setattr(awake_module, "market_is_open", lambda now=None: True)
    keeper.reconcile()
    assert keeper.running() is True

    status = keeper.set_enabled(False)

    assert keeper.running() is False
    assert status["enabled"] is False
    assert awake_module.keep_awake_enabled() is False


def test_the_switch_says_so_when_the_machine_is_not_a_mac(prefs, monkeypatch):
    monkeypatch.setattr(awake_module, "supported", lambda: False)
    keeper = AwakeKeeper(command=["sleep", "60"])

    status = keeper.status()

    assert status["supported"] is False
    assert status["active"] is False
    assert "Only available on a Mac" in status["text"]


def test_the_api_exposes_the_toggle(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient

    from intraday import credentials as credentials_module
    from intraday.web import app as web_app

    monkeypatch.setattr(credentials_module, "CREDENTIALS_FILE", tmp_path / "credentials.json")
    monkeypatch.setattr(awake_module, "PREFS_FILE", tmp_path / "preferences.json")
    monkeypatch.setattr(awake_module, "_keeper", AwakeKeeper(command=["sleep", "60"]))

    with TestClient(web_app.app) as client:
        assert client.get("/api/keep-awake").json()["enabled"] is True
        off = client.post("/api/keep-awake", json={"enabled": False}).json()
        assert off["enabled"] is False
        assert "market hours" in off["label"]
        assert client.get("/api/system").json()["keep_awake"]["enabled"] is False
