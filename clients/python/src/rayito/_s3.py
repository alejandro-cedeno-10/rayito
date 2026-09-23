"""Adaptador boto3 de las transferencias (ADR-010): las firmas con las
credenciales del llamante y las llamadas del SDK directas a S3.

Sólo usa las operaciones y parámetros de `AWS_API_NOTES.md` §18
(`generate_presigned_url`, `generate_presigned_post`,
`create_multipart_upload`/`complete_multipart_upload`/`abort_multipart_upload`,
`upload_fileobj`, `get_object`, `delete_object`). Es bloqueante: los dos
árboles lo llaman desde un hilo acotado por el plazo de la operación
(`run_bounded`, `asyncio.to_thread`). Nunca registra una URL, un bucket, una
clave ni una ruta.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable, Iterator, Sequence
from typing import Any, TypeVar

import boto3
from boto3.s3.transfer import TransferConfig
from botocore.exceptions import BotoCoreError, ClientError

from rayito._filesystem_base import guarded_messages
from rayito._transfer_base import (
    OBJECT_READ_CHUNK_BYTES,
    OCTET_STREAM,
    UPLOAD_CHUNK_BYTES,
    UPLOAD_MAX_CONCURRENCY,
    HashingReader,
    ImportUrls,
    StagingObject,
    completed_parts,
    download_params,
    object_params,
    post_conditions,
    presign_client_config,
    transfer_timeout_error,
    translate_s3_error,
    upload_part_params,
)

logger = logging.getLogger("rayito.transfer")

T = TypeVar("T")


def s3_call(invoke: Callable[[], T]) -> T:
    try:
        return invoke()
    except (ClientError, BotoCoreError) as exc:
        raise translate_s3_error(exc) from exc


class S3Gateway:
    """Un cliente `s3` de la sesión boto3 del sandbox con
    `presign_client_config()`; firma y transfiere en la región del bucket."""

    def __init__(self, client: Any) -> None:
        self._client = client

    @classmethod
    def from_session(cls, session: boto3.session.Session | None, region: str) -> S3Gateway:
        resolved = session or boto3.session.Session()
        return cls(resolved.client("s3", region_name=region, config=presign_client_config()))

    # ----------------------------------------------------------- presigning

    def presign_put(self, target: StagingObject, lifetime: int) -> str:
        return self._presign("put_object", object_params(target), lifetime)

    def presign_get(self, target: StagingObject, lifetime: int) -> str:
        return self._presign("get_object", object_params(target), lifetime)

    def presign_delete(self, target: StagingObject, lifetime: int) -> str:
        return self._presign("delete_object", object_params(target), lifetime)

    def presign_download(self, target: StagingObject, lifetime: int, disposition: str) -> str:
        return self._presign("get_object", download_params(target, disposition), lifetime)

    def presign_upload_parts(
        self, target: StagingObject, upload_id: str, part_count: int, lifetime: int
    ) -> list[str]:
        return [
            self._presign("upload_part", upload_part_params(target, upload_id, number), lifetime)
            for number in range(1, part_count + 1)
        ]

    def presign_post(
        self, target: StagingObject, max_bytes: int | None, lifetime: int
    ) -> tuple[str, dict[str, str]]:
        """El formulario POST con `content-length-range [0, max_bytes o 5 GiB]`:
        S3 rechaza con `EntityTooLarge` lo que lo supere antes de guardarlo."""
        form = s3_call(
            lambda: self._client.generate_presigned_post(
                Bucket=target.bucket,
                Key=target.key,
                Fields=None,
                Conditions=post_conditions(max_bytes),
                ExpiresIn=lifetime,
            )
        )
        return str(form["url"]), {str(name): str(value) for name, value in form["fields"].items()}

    def presign_user_upload(
        self, target: StagingObject, *, form: bool, max_bytes: int | None, lifetime: int
    ) -> tuple[str, dict[str, str]]:
        """La URL que recibe el caller: un PUT crudo o el formulario POST."""
        if form:
            return self.presign_post(target, max_bytes, lifetime)
        return self.presign_put(target, lifetime), {}

    def presign_import(
        self, target: StagingObject, *, get_lifetime: int, delete_lifetime: int
    ) -> ImportUrls:
        """El GET que sondea `rayd` y el DELETE con el que limpia el objeto."""
        return ImportUrls(
            get_url=self.presign_get(target, get_lifetime),
            delete_url=self.presign_delete(target, delete_lifetime),
        )

    def _presign(self, method: str, params: dict[str, Any], lifetime: int) -> str:
        return str(
            s3_call(
                lambda: self._client.generate_presigned_url(
                    ClientMethod=method, Params=params, ExpiresIn=lifetime
                )
            )
        )

    # ------------------------------------------------------------ multipart

    def create_multipart_upload(self, target: StagingObject) -> str:
        response = s3_call(
            lambda: self._client.create_multipart_upload(
                Bucket=target.bucket, Key=target.key, ContentType=OCTET_STREAM
            )
        )
        return str(response["UploadId"])

    def complete_multipart_upload(
        self, target: StagingObject, upload_id: str, etags: Sequence[str]
    ) -> None:
        s3_call(
            lambda: self._client.complete_multipart_upload(
                Bucket=target.bucket,
                Key=target.key,
                UploadId=upload_id,
                MultipartUpload=completed_parts(etags),
            )
        )

    def abort_multipart_upload_quietly(self, target: StagingObject, upload_id: str) -> None:
        """Best effort tras un fallo: el error original es el que importa y la
        regla `AbortIncompleteMultipartUpload` del bucket recoge lo que quede."""
        try:
            s3_call(
                lambda: self._client.abort_multipart_upload(
                    Bucket=target.bucket, Key=target.key, UploadId=upload_id
                )
            )
        except Exception as exc:
            logger.warning("no se pudo abortar una subida multiparte: %s", type(exc).__name__)

    # ------------------------------------------------------------- objects

    def upload(self, reader: HashingReader, target: StagingObject) -> None:
        s3_call(
            lambda: self._client.upload_fileobj(
                Fileobj=reader,
                Bucket=target.bucket,
                Key=target.key,
                ExtraArgs={"ContentType": OCTET_STREAM},
                Config=TransferConfig(
                    multipart_chunksize=UPLOAD_CHUNK_BYTES, max_concurrency=UPLOAD_MAX_CONCURRENCY
                ),
            )
        )

    def open_chunks(self, target: StagingObject) -> ObjectChunks:
        """El cuerpo de `get_object` en trozos de 256 KiB, como `Read`."""
        response = s3_call(lambda: self._client.get_object(Bucket=target.bucket, Key=target.key))
        body = response["Body"]
        return ObjectChunks(body)

    def delete_quietly(self, target: StagingObject) -> None:
        """Best effort: si falla, la regla de ciclo de vida de un día lo borra."""
        try:
            s3_call(lambda: self._client.delete_object(Bucket=target.bucket, Key=target.key))
        except Exception as exc:
            logger.warning("no se pudo borrar un objeto de staging: %s", type(exc).__name__)


class ObjectChunks:
    """Iterador sobre un `StreamingBody` que traduce los errores de botocore
    y se puede cerrar (la guardia de inactividad lo cierra para cortar)."""

    def __init__(self, body: Any) -> None:
        self._body = body
        self._chunks: Iterator[bytes] = body.iter_chunks(OBJECT_READ_CHUNK_BYTES)

    def __iter__(self) -> ObjectChunks:
        return self

    def __next__(self) -> bytes:
        return bytes(s3_call(lambda: next(self._chunks)))

    def close(self) -> None:
        self._body.close()


class ObjectFetch:
    """La descarga entera de un objeto de staging (`format="bytes"`/`"text"`)
    con `stream_idle_timeout` entre trozos y cancelable desde otro hilo:
    `cancel` cierra el cuerpo, así un `get_object` que se cuelga no sigue
    leyendo en segundo plano cuando la operación ya agotó su plazo."""

    def __init__(self, gateway: S3Gateway, target: StagingObject, idle: float | None) -> None:
        self._gateway = gateway
        self._target = target
        self._idle = idle
        self._lock = threading.Lock()
        self._chunks: ObjectChunks | None = None
        self._cancelled = False

    def run(self) -> bytes:
        chunks = self._gateway.open_chunks(self._target)
        with self._lock:
            if self._cancelled:
                chunks.close()
                raise transfer_timeout_error()
            self._chunks = chunks
        try:
            return b"".join(guarded_messages(chunks, self._idle, chunks.close))
        finally:
            chunks.close()

    def cancel(self) -> None:
        with self._lock:
            self._cancelled = True
            chunks = self._chunks
        if chunks is not None:
            chunks.close()
