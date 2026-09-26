"""Fetch pre-trained model bundles from a GitHub release.

Training three years of 1-minute data on a laptop takes hours. The owner's
first run instead downloads the bundle that was trained elsewhere, checks its
SHA-256 against the checksum published next to it, and unpacks the joblib
files into `~/.intraday/models`. If the asset is missing or the checksum does
not match, nothing is installed and the caller falls back to training locally
— slower, but never silently wrong.

The archive is expected to contain `<target>.joblib` files plus the
`validation_report.json` that the dashboard reads. Nothing here changes what
those models are allowed to do: a target whose report says UNVALIDATED still
cannot produce a BUY.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import tarfile
import tempfile
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from intraday.config import MODEL_DIR, ensure_dirs

logger = logging.getLogger(__name__)

# Fixed, well-defined location. The tag is stable so the installer never has
# to be edited when a new bundle is published: the release assets are replaced.
RELEASE_TAG = os.environ.get("INTRADAY_MODEL_RELEASE", "models-latest")
REPO = os.environ.get("INTRADAY_MODEL_REPO", "developkhanna/INTRADAY")
BUNDLE_NAME = "models.tar.gz"
BASE_URL = f"https://github.com/{REPO}/releases/download/{RELEASE_TAG}"
BUNDLE_URL = os.environ.get("INTRADAY_MODEL_URL", f"{BASE_URL}/{BUNDLE_NAME}")
CHECKSUM_URL = os.environ.get("INTRADAY_MODEL_SHA_URL", f"{BUNDLE_URL}.sha256")

MARKER = MODEL_DIR / "bundle.json"
TIMEOUT_SECONDS = 120
MAX_BYTES = 2 * 1024 * 1024 * 1024  # a corrupt or hostile archive cannot fill the disk


@dataclass(frozen=True)
class BundleResult:
    ok: bool
    message: str
    files: int = 0
    sha256: str = ""


def bundle_installed(path: Path | None = None) -> bool:
    path = path or MARKER
    return path.exists()


def installed_bundle(path: Path | None = None) -> dict | None:
    path = path or MARKER
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return None


def _fetch(url: str) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": "intraday-installer"})
    with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:  # noqa: S310
        return response.read(MAX_BYTES)


def _safe_members(archive: tarfile.TarFile, destination: Path):
    """Only plain files, only inside the destination. No symlinks, no ../."""
    for member in archive.getmembers():
        if not member.isfile():
            continue
        target = (destination / member.name).resolve()
        if not str(target).startswith(str(destination.resolve())):
            raise ValueError(f"archive entry escapes the models folder: {member.name}")
        member.name = Path(member.name).name  # flatten: models live in one folder
        yield member


def download_bundle(
    url: str | None = None,
    checksum_url: str | None = None,
    destination: Path | None = None,
) -> BundleResult:
    """Download, verify and unpack the pre-trained models. Never raises."""
    url = url or BUNDLE_URL
    checksum_url = checksum_url or CHECKSUM_URL
    destination = destination or MODEL_DIR
    ensure_dirs()
    destination.mkdir(parents=True, exist_ok=True)

    try:
        expected = _fetch(checksum_url).decode("utf-8", "replace").split()[0].strip().lower()
    except (urllib.error.URLError, OSError, IndexError, ValueError) as exc:
        return BundleResult(
            False,
            "No pre-trained models are published yet "
            f"({type(exc).__name__}). Training locally instead.",
        )

    try:
        payload = _fetch(url)
    except (urllib.error.URLError, OSError) as exc:
        return BundleResult(
            False,
            f"Could not download the pre-trained models ({type(exc).__name__}). "
            "Training locally instead.",
        )

    actual = hashlib.sha256(payload).hexdigest()
    if actual != expected:
        return BundleResult(
            False,
            "The downloaded models did not match their published checksum, so they "
            "were discarded. Training locally instead.",
        )

    with tempfile.TemporaryDirectory() as work:
        archive_path = Path(work) / BUNDLE_NAME
        archive_path.write_bytes(payload)
        staging = Path(work) / "models"
        staging.mkdir()
        try:
            with tarfile.open(archive_path, "r:gz") as archive:
                archive.extractall(staging, members=_safe_members(archive, staging))
        except (tarfile.TarError, ValueError, OSError) as exc:
            return BundleResult(
                False, f"The downloaded models could not be unpacked ({type(exc).__name__})."
            )

        extracted = sorted(p for p in staging.iterdir() if p.is_file())
        if not extracted:
            return BundleResult(False, "The published bundle was empty.")
        for item in extracted:
            shutil.copy2(item, destination / item.name)

    MARKER.write_text(
        json.dumps(
            {
                "url": url,
                "sha256": actual,
                "files": [p.name for p in extracted],
                "release_tag": RELEASE_TAG,
            },
            indent=2,
        )
    )
    return BundleResult(
        True,
        f"Installed {len(extracted)} pre-trained model files. "
        "Their published validation status is unchanged.",
        files=len(extracted),
        sha256=actual,
    )


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    result = download_bundle()
    print(result.message)
    return 0 if result.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
