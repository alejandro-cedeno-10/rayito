"""Descarga de la release del SDK (`rayd-v<versión>`): `SHA256SUMS` y
`rayito-image.zip` por HTTPS con `urllib` (biblioteca estándar, sin
dependencias nuevas; honra `HTTPS_PROXY`). El zip se escribe a disco por
trozos y su sha256 se calcula mientras se escribe. Ningún mensaje de error
lleva la URL."""

from __future__ import annotations

import hashlib
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import Any, BinaryIO, Final

from rayito._account_setup._domain import (
    CHECKSUMS_ASSET_NAME,
    DOWNLOAD_REASONS,
    IMAGE_ASSET_NAME,
    RELEASE_DOWNLOAD_TIMEOUT_SECONDS,
    AccountSetupException,
    DownloadedRelease,
    progress_message,
    release_asset_url,
)
from rayito._account_setup._ports import Progress

#: Tamaño de cada lectura al descargar: 1 MiB, un trozo que no llena la
#: memoria con un zip de decenas de MiB.
DOWNLOAD_CHUNK_BYTES: Final = 1 << 20

Opener = Callable[..., Any]
Clock = Callable[[], float]


class GithubReleaseSource:
    """`ReleaseArtifactSource` sobre las descargas de GitHub Releases.
    Construirlo no hace ninguna llamada de red."""

    def __init__(
        self,
        *,
        opener: Opener = urllib.request.urlopen,
        timeout_seconds: float = RELEASE_DOWNLOAD_TIMEOUT_SECONDS,
        clock: Clock = time.monotonic,
    ) -> None:
        self._opener = opener
        self._timeout = timeout_seconds
        self._clock = clock

    def fetch(self, tag: str, directory: Path, on_progress: Progress) -> DownloadedRelease:
        checksums = bytearray()
        self._download(tag, CHECKSUMS_ASSET_NAME, checksums.extend, on_progress)
        digest = hashlib.sha256()
        artifact = directory / IMAGE_ASSET_NAME
        with artifact.open("wb") as sink:
            self._download(tag, IMAGE_ASSET_NAME, _hashing_writer(sink, digest.update), on_progress)
        return DownloadedRelease(
            checksums_text=checksums.decode("utf-8", errors="replace"),
            artifact=artifact,
            artifact_sha256=digest.hexdigest(),
        )

    def _download(
        self, tag: str, asset: str, consume: Callable[[bytes], object], on_progress: Progress
    ) -> None:
        on_progress(progress_message("download", asset=asset, release=tag))
        started = self._clock()
        try:
            with self._opener(release_asset_url(tag, asset), timeout=self._timeout) as response:
                while chunk := response.read(DOWNLOAD_CHUNK_BYTES):
                    consume(chunk)
                    if self._clock() - started > self._timeout:
                        raise TimeoutError
        except urllib.error.HTTPError as exc:
            raise _download_error(tag, asset, "http", status=str(exc.code)) from None
        except TimeoutError:
            raise _download_error(tag, asset, "timeout") from None
        except urllib.error.URLError as exc:
            kind = "timeout" if isinstance(exc.reason, TimeoutError) else "network"
            raise _download_error(tag, asset, kind) from None
        except OSError:
            raise _download_error(tag, asset, "network") from None


def _hashing_writer(
    sink: BinaryIO, update_digest: Callable[[bytes], object]
) -> Callable[[bytes], object]:
    def write(chunk: bytes) -> None:
        update_digest(chunk)
        sink.write(chunk)

    return write


def _download_error(tag: str, asset: str, reason: str, **fields: str) -> AccountSetupException:
    return AccountSetupException(
        "release_download",
        asset=asset,
        release=tag,
        reason=DOWNLOAD_REASONS[reason].format(**fields),
    )
