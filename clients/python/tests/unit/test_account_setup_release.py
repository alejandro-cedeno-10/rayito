"""`GithubReleaseSource` con un `opener` falso: URLs exactas, el zip a disco
por trozos con su sha256, y un error cerrado (sin la URL) para HTTP, plazo y
red."""

from __future__ import annotations

import hashlib
import io
import urllib.error
from pathlib import Path
from typing import Any

import pytest

from rayito._account_setup import _release
from rayito._account_setup._domain import AccountSetupException
from rayito._account_setup._release import GithubReleaseSource

TAG = "rayd-v1.2.3"
BASE_URL = f"https://github.com/alejandro-cedeno-10/rayito/releases/download/{TAG}"
CHECKSUMS_URL = f"{BASE_URL}/SHA256SUMS"
ZIP_URL = f"{BASE_URL}/rayito-image.zip"
ZIP_BYTES = b"0123456789" * 5
TIMEOUT = 600.0


class FakeResponse:
    def __init__(self, body: bytes) -> None:
        self._body = io.BytesIO(body)

    def __enter__(self) -> FakeResponse:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def read(self, size: int) -> bytes:
        return self._body.read(size)


class FakeOpener:
    def __init__(self, bodies: dict[str, bytes], failures: dict[str, BaseException] | None = None):
        self.bodies = bodies
        self.failures = failures or {}
        self.calls: list[tuple[str, float]] = []

    def __call__(self, url: str, *, timeout: float) -> FakeResponse:
        self.calls.append((url, timeout))
        if url in self.failures:
            raise self.failures[url]
        return FakeResponse(self.bodies[url])


def healthy(**overrides: Any) -> FakeOpener:
    bodies = {CHECKSUMS_URL: b"abc  rayito-image.zip\n", ZIP_URL: ZIP_BYTES}
    return FakeOpener(bodies, **overrides)


def fetch_error(opener: FakeOpener, tmp_path: Path, **source: Any) -> AccountSetupException:
    with pytest.raises(AccountSetupException) as caught:
        GithubReleaseSource(opener=opener, **source).fetch(TAG, tmp_path, lambda _line: None)
    return caught.value


def test_fetch_downloads_both_assets_from_the_exact_urls(tmp_path: Path) -> None:
    opener = healthy()
    GithubReleaseSource(opener=opener).fetch(TAG, tmp_path, lambda _line: None)
    assert opener.calls == [(CHECKSUMS_URL, TIMEOUT), (ZIP_URL, TIMEOUT)]


def test_fetch_streams_the_zip_to_disk_and_hashes_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(_release, "DOWNLOAD_CHUNK_BYTES", 7)
    release = GithubReleaseSource(opener=healthy()).fetch(TAG, tmp_path, lambda _line: None)
    assert release.artifact == tmp_path / "rayito-image.zip"
    assert release.artifact.read_bytes() == ZIP_BYTES
    assert release.artifact_sha256 == hashlib.sha256(ZIP_BYTES).hexdigest()
    assert release.checksums_text == "abc  rayito-image.zip\n"


def test_fetch_reports_each_download(tmp_path: Path) -> None:
    lines: list[str] = []
    GithubReleaseSource(opener=healthy()).fetch(TAG, tmp_path, lines.append)
    assert lines == [
        f"descargando SHA256SUMS de {TAG}",
        f"descargando rayito-image.zip de {TAG}",
    ]


def test_an_http_error_names_the_status_and_never_the_url(tmp_path: Path) -> None:
    failure = urllib.error.HTTPError(ZIP_URL, 404, "Not Found", None, None)  # type: ignore[arg-type]
    error = fetch_error(healthy(failures={ZIP_URL: failure}), tmp_path)
    assert error.code == "release_download"
    assert str(error) == (f"no se pudo descargar rayito-image.zip de la release {TAG}: HTTP 404")
    assert "https://" not in str(error)
    assert error.__cause__ is None


def test_a_failure_on_the_checksums_stops_before_the_zip(tmp_path: Path) -> None:
    failure = urllib.error.HTTPError(CHECKSUMS_URL, 500, "Boom", None, None)  # type: ignore[arg-type]
    opener = healthy(failures={CHECKSUMS_URL: failure})
    error = fetch_error(opener, tmp_path)
    assert "SHA256SUMS" in str(error)
    assert [url for url, _timeout in opener.calls] == [CHECKSUMS_URL]


@pytest.mark.parametrize(
    ("failure", "reason"),
    [
        (TimeoutError(), "tiempo agotado"),
        (urllib.error.URLError(TimeoutError()), "tiempo agotado"),
        (urllib.error.URLError("unreachable"), "error de red"),
        (ConnectionResetError(), "error de red"),
    ],
)
def test_timeouts_and_network_errors_have_their_own_reason(
    tmp_path: Path, failure: BaseException, reason: str
) -> None:
    error = fetch_error(healthy(failures={ZIP_URL: failure}), tmp_path)
    assert str(error).endswith(f": {reason}")
    assert "https://" not in str(error)


def test_the_deadline_is_enforced_per_asset_between_chunks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(_release, "DOWNLOAD_CHUNK_BYTES", 7)
    ticks = iter([0.0, 5.0, 11.0, 12.0, 13.0])
    error = fetch_error(healthy(), tmp_path, timeout_seconds=10.0, clock=lambda: next(ticks))
    assert str(error).endswith(": tiempo agotado")


def test_construction_makes_no_network_call() -> None:
    opener = healthy()
    GithubReleaseSource(opener=opener)
    assert opener.calls == []
