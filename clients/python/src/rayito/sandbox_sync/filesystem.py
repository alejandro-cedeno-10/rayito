"""`Sandbox.files`: `FilesystemService` sobre `grpc`.

Los unarios, `Write` y el `Read` en foreground van por el canal de unarios;
`WatchDir` por el de streams largos. `read` hace `Stat` antes de abrir el
stream para fijar el deadline por tamaño y rechazar directorios y symlinks
sin tocar al agente. `Write` escribe cada fichero en un temporal del mismo
directorio y lo renombra: en un directorio observado aparece como un único
`WRITE` del destino (el agente empareja el rename del temporal), con
`entry` si se pidió `include_entry`. Un `WatchHandle` cuyo stream cierra
un `/suspend` (o el proxy) espera al agente y vuelve a emitir `WatchDir` con
los mismos parámetros; los eventos ocurridos durante la pausa se pierden.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Iterator, Mapping, Sequence
from typing import IO, TYPE_CHECKING, Any, Literal, overload

import grpc

from rayito._filesystem_base import (
    WATCH_STOP_JOIN_SECONDS,
    EventCallback,
    ExitCallback,
    WatchState,
    build_write_requests,
    decode_text,
    entry_info_from_proto,
    file_request_deadline,
    guarded_messages,
    is_already_exists,
    list_dir_request,
    make_dir_request,
    move_request,
    next_watch_name,
    notify_exit,
    read_call_options,
    read_request,
    remove_request,
    require_regular_file,
    require_watch_started,
    stat_request,
    total_write_bytes,
    validate_metadata,
    validate_read_format,
    validate_stream_idle_timeout,
    validate_watch_timeout,
    watch_dir_request,
    watch_failure,
    write_call_options,
)
from rayito._limits import TRANSFER_DEFAULT_EXPIRES_IN_SECONDS
from rayito._models import DownloadLink, EntryInfo, FilesystemEvent, UploadTicket, WriteEntry
from rayito._process_base import deadline_at, remaining_deadline
from rayito._sandbox_base import GateRetry, ReconnectBudget
from rayito._transfer_base import WritePlan, plan_writes
from rayito._transport import is_stream_reset, translate_rpc_error
from rayito.exceptions import FileNotFoundException, SandboxException, TimeoutException
from rayito.v1 import filesystem_pb2, filesystem_pb2_grpc

if TYPE_CHECKING:
    from rayito.sandbox_sync.main import Sandbox

FILES_STUB = filesystem_pb2_grpc.FilesystemServiceStub

logger = logging.getLogger("rayito.files")


class Filesystem:
    """Ficheros del sandbox (`FilesystemService`)."""

    def __init__(self, sandbox: Sandbox) -> None:
        self._sandbox = sandbox
        self._watches: set[WatchHandle] = set()
        self._lock = threading.Lock()

    @overload
    def read(
        self,
        path: str,
        *,
        format: Literal["text"] = "text",
        gzip: bool = False,
        stream_idle_timeout: float | None = None,
        user: str | None = None,
        request_timeout: float | None = None,
    ) -> str: ...

    @overload
    def read(
        self,
        path: str,
        *,
        format: Literal["bytes"],
        gzip: bool = False,
        stream_idle_timeout: float | None = None,
        user: str | None = None,
        request_timeout: float | None = None,
    ) -> bytes: ...

    @overload
    def read(
        self,
        path: str,
        *,
        format: Literal["stream"],
        gzip: bool = False,
        stream_idle_timeout: float | None = None,
        user: str | None = None,
        request_timeout: float | None = None,
    ) -> Iterator[bytes]: ...

    def read(
        self,
        path: str,
        *,
        format: str = "text",
        gzip: bool = False,
        stream_idle_timeout: float | None = None,
        user: str | None = None,
        request_timeout: float | None = None,
    ) -> str | bytes | Iterator[bytes]:
        """Lee un fichero regular: `text` (UTF-8 estricto), `bytes` o `stream`
        (chunks de hasta 256 KiB tal como llegan). Hace `Stat` primero: un
        directorio o un symlink es `InvalidArgumentException` sin abrir el
        stream, y el deadline del `Read` es `60 s + 1 s por MB` del tamaño
        salvo `request_timeout`.

        `gzip=True` pide la respuesta comprimida (inocuo en una imagen
        anterior, que responde sin comprimir). `stream_idle_timeout` (s)
        cancela la lectura si el siguiente chunk tarda más y levanta
        `TimeoutException`. Con `transfer=S3Staging(...)` un fichero de al
        menos `threshold_bytes` se exporta a S3 y se descarga con tus
        credenciales (verificando su sha256); `gzip` no aplica ahí."""
        read_format = validate_read_format(format)
        idle = validate_stream_idle_timeout(stream_idle_timeout)
        request = read_request(path, user)
        entry = require_regular_file(
            self.get_info(path, user=user, request_timeout=request_timeout)
        )
        if self._routes(entry.size):
            return self._sandbox._transfers.read_routed(
                path,
                entry,
                read_format=read_format,
                user=user,
                request_timeout=request_timeout,
                idle=idle,
            )
        chunks = self._read_chunks(
            request,
            file_request_deadline(entry.size, request_timeout),
            options=read_call_options(gzip),
            idle=idle,
        )
        if read_format == "stream":
            return chunks
        data = b"".join(chunks)
        return data if read_format == "bytes" else decode_text(data)

    def write(
        self,
        path: str,
        data: str | bytes | IO[bytes] | IO[str],
        *,
        user: str | None = None,
        mode: int | None = None,
        gzip: bool = False,
        metadata: Mapping[str, str] | None = None,
        use_octet_stream: bool = False,
        request_timeout: float | None = None,
    ) -> EntryInfo:
        """Escribe `data` (str en UTF-8, bytes o fichero abierto) de forma
        atómica, creando los padres y con `mode` (0o644 por defecto). Los
        padres que falten se crean; el propietario es `user`. `gzip`,
        `metadata` y `use_octet_stream` como en `write_files`."""
        entries = self.write_files(
            [WriteEntry(path, data, mode)],
            user=user,
            gzip=gzip,
            metadata=metadata,
            use_octet_stream=use_octet_stream,
            request_timeout=request_timeout,
        )
        return entries[0]

    def write_files(
        self,
        files: Sequence[WriteEntry],
        *,
        user: str | None = None,
        gzip: bool = False,
        metadata: Mapping[str, str] | None = None,
        use_octet_stream: bool = False,
        request_timeout: float | None = None,
    ) -> list[EntryInfo]:
        """N ficheros en **un** stream `Write`; devuelve sus `EntryInfo` en
        orden. Cada fichero se compromete de forma atómica por separado: si
        el stream falla a mitad, los ya escritos quedan y el resto no existe.
        Deadline `60 s + 1 s por MB` del total salvo `request_timeout`.

        `gzip=True` comprime el stream (`grpc-encoding: gzip`). `metadata`
        (claves token de HTTP, se guardan en minúsculas) se aplica a cada
        fichero y sustituye el conjunto entero; se valida antes de cualquier
        RPC. Ambos exigen un agente M9 (`UnimplementedError` si no).
        `use_octet_stream` se acepta y no tiene efecto (gRPC no usa
        formularios). Con `transfer=S3Staging(...)`, cada fichero de al menos
        `threshold_bytes` (o un stream binario no buscable) se sube a S3 con
        tus credenciales y `rayd` lo importa; `gzip` no aplica ahí."""
        normalized = validate_metadata(metadata)
        self._require_m9_write(gzip=gzip, metadata=normalized)
        plan = self._plan(files)
        results: dict[int, EntryInfo] = {}
        if plan.grpc:
            prepared = plan.grpc_entries
            deadline = file_request_deadline(total_write_bytes(prepared), request_timeout)
            response = self._sandbox._files_call(
                lambda stub, timeout: stub.Write(
                    build_write_requests(prepared, user, normalized),
                    timeout=timeout,
                    **write_call_options(gzip),
                ),
                deadline,
            )
            for (index, _), entry in zip(plan.grpc, response.entries, strict=False):
                results[index] = entry_info_from_proto(entry)
        for routed in plan.routed:
            results[routed.index] = self._sandbox._transfers.write_routed(
                routed, user=user, metadata=normalized, request_timeout=request_timeout
            )
        return [results[index] for index in range(plan.count)]

    def upload_url(
        self,
        path: str,
        *,
        user: str | None = None,
        expires_in: int = TRANSFER_DEFAULT_EXPIRES_IN_SECONDS,
        max_bytes: int | None = None,
        form: bool = False,
        request_timeout: float | None = None,
    ) -> UploadTicket:
        """Una URL de subida a S3 firmada con tus credenciales, con la
        importación a `path` ya armada en el sandbox (ADR-010). Es un `str`:
        `requests.put(ticket, data=f, headers=ticket.headers)` basta; con
        `form=True` es un formulario POST (`ticket.fields` más el fichero en
        `file`) que S3 limita a `max_bytes`. De un solo uso; `expires_in` se
        topa en `min(expires_in, transfer.max_expires_in, 604800)`. El
        fichero aparece de forma asíncrona: `read`, `get_info`, `list`, un
        comando o una celda esperan a la importación, y `ticket.wait()` la
        espera explícitamente. Sin `transfer=S3Staging(...)` ni
        `RAYITO_TRANSFER_BUCKET` levanta `UnimplementedError`."""
        return self._sandbox._transfers.upload_url(
            path,
            user=user,
            expires_in=expires_in,
            max_bytes=max_bytes,
            form=form,
            request_timeout=request_timeout,
        )

    def download_url(
        self,
        path: str,
        *,
        user: str | None = None,
        expires_in: int = TRANSFER_DEFAULT_EXPIRES_IN_SECONDS,
        filename: str | None = None,
        request_timeout: float | None = None,
    ) -> DownloadLink:
        """Exporta `path` tal como está ahora a S3 y devuelve una URL de
        descarga firmada con tus credenciales (un `str` con `size`, `sha256`
        y `expires_at`); `urlopen(link)` basta y admite `Range`. Un fichero
        inexistente levanta `FileNotFoundException` al llamar; un directorio
        o un symlink, `InvalidArgumentException`. `filename` es el nombre de
        `Content-Disposition` (por defecto el de `path`)."""
        return self._sandbox._transfers.download_url(
            path,
            user=user,
            expires_in=expires_in,
            filename=filename,
            request_timeout=request_timeout,
        )

    def list(
        self,
        path: str,
        *,
        depth: int = 1,
        user: str | None = None,
        request_timeout: float | None = None,
    ) -> list[EntryInfo]:
        """Entradas hasta `depth` niveles (1 = sólo hijos directos; 0 vale 1)
        en preorden, ordenadas por nombre en cada directorio, sin seguir
        symlinks. Más de 10 000 entradas es `RateLimitException`."""
        request = list_dir_request(path, depth, user)
        response = self._sandbox._files_call(
            lambda stub, timeout: stub.ListDir(request, timeout=timeout), request_timeout
        )
        return [entry_info_from_proto(entry) for entry in response.entries]

    def exists(
        self, path: str, *, user: str | None = None, request_timeout: float | None = None
    ) -> bool:
        """False sólo si `Stat` responde `NOT_FOUND`; otros errores se propagan."""
        try:
            self.get_info(path, user=user, request_timeout=request_timeout)
        except FileNotFoundException:
            return False
        return True

    def get_info(
        self, path: str, *, user: str | None = None, request_timeout: float | None = None
    ) -> EntryInfo:
        """`Stat` sin seguir symlinks (`type is FileType.SYMLINK` con `symlink_target`)."""
        request = stat_request(path, user)
        response = self._sandbox._files_call(
            lambda stub, timeout: stub.Stat(request, timeout=timeout), request_timeout
        )
        return entry_info_from_proto(response.entry)

    def remove(
        self,
        path: str,
        *,
        recursive: bool = True,
        user: str | None = None,
        request_timeout: float | None = None,
    ) -> None:
        """Borra un fichero, symlink (sólo el enlace) o directorio; con
        `recursive=False` un directorio no vacío es `InvalidArgumentException`."""
        request = remove_request(path, recursive, user)
        self._sandbox._files_call(
            lambda stub, timeout: stub.Remove(request, timeout=timeout), request_timeout
        )

    def rename(
        self,
        old_path: str,
        new_path: str,
        *,
        user: str | None = None,
        request_timeout: float | None = None,
    ) -> EntryInfo:
        """`rename(2)`: reemplaza un fichero regular existente; un destino que
        es un directorio no vacío es `InvalidArgumentException`."""
        request = move_request(old_path, new_path, user)
        response = self._sandbox._files_call(
            lambda stub, timeout: stub.Move(request, timeout=timeout), request_timeout
        )
        return entry_info_from_proto(response.entry)

    def make_dir(
        self, path: str, *, user: str | None = None, request_timeout: float | None = None
    ) -> bool:
        """Crea el directorio y sus padres (0o755). True si lo creó, False si
        ya existía; si existe algo que no es un directorio,
        `InvalidArgumentException`."""
        request = make_dir_request(path, user)
        timeout = self._sandbox._resolve_request_timeout(request_timeout)
        try:
            self._sandbox._call_unary(
                lambda: self._sandbox._files.MakeDir(request, timeout=timeout)
            )
        except grpc.RpcError as exc:
            if is_already_exists(exc):
                return False
            raise translate_rpc_error(exc, filesystem=True) from exc
        return True

    def watch_dir(
        self,
        path: str,
        *,
        on_event: EventCallback | None = None,
        on_exit: ExitCallback | None = None,
        recursive: bool = False,
        include_entry: bool = False,
        user: str | None = None,
        timeout: float | None = 0,
        request_timeout: float | None = None,
    ) -> WatchHandle:
        """Observa un directorio (inotify, sin debounce). Bloquea hasta
        `WatchStarted` y devuelve un `WatchHandle` que consume el stream en
        un hilo daemon: con `on_event` cada evento va al callback; sin él se
        acumulan para `get_new_events()`. `timeout` (> 0) es el deadline gRPC
        del stream y termina en `TimeoutException`; `0`/`None` es ilimitado.
        `request_timeout` se acepta por uniformidad y no aplica al stream. Un
        `files.write` en el directorio aparece como un único `WRITE`."""
        request = watch_dir_request(path, recursive, include_entry, user)
        deadline = validate_watch_timeout(timeout)
        call = self._open_watch(request, deadline)
        handle = WatchHandle(
            filesystem=self,
            call=call,
            path=path,
            state=WatchState(on_event),
            on_exit=on_exit,
            request=request,
            deadline_at=deadline_at(deadline, time.monotonic),
        )
        self._track(handle)
        handle._start()
        return handle

    def _open_watch(self, request: filesystem_pb2.WatchDirRequest, deadline: float | None) -> Any:
        """`WatchDir` ya consumido hasta su `WatchStarted`; también es lo que
        un `WatchHandle` reabre tras una reconexión."""
        call, first = self._sandbox._open_stream(
            lambda stub: stub.WatchDir(request, timeout=deadline),
            service=FILES_STUB,
            stream=True,
            filesystem=True,
        )
        try:
            require_watch_started(first)
        except SandboxException:
            call.cancel()
            raise
        return call

    def _read_chunks(
        self,
        request: Any,
        deadline: float,
        *,
        options: dict[str, Any] | None = None,
        idle: float | None = None,
    ) -> Iterator[bytes]:
        call, first = self._sandbox._open_stream(
            lambda stub: stub.Read(request, timeout=deadline, **(options or {})),
            service=FILES_STUB,
            stream=False,
            allow_empty=True,
            filesystem=True,
        )
        return self._remaining_chunks(call, first, idle)

    def _remaining_chunks(
        self, call: Any, first: Any, idle: float | None = None
    ) -> Iterator[bytes]:
        """Abandonar el iterador cancela el RPC para que el agente deje de
        bombear; con `idle`, una espera más larga entre chunks lo cancela y
        levanta `TimeoutException`."""
        if first is None:
            return
        try:
            yield bytes(first.chunk)
            for response in guarded_messages(iter(call), idle, call.cancel):
                yield bytes(response.chunk)
        except grpc.RpcError as exc:
            raise self._stream_failure(exc) from exc
        finally:
            call.cancel()

    def _stream_failure(self, exc: grpc.RpcError) -> Exception:
        return self._sandbox._stream_failure(exc, filesystem=True)

    def _routes(self, size: int) -> bool:
        staging = self._sandbox.transfer
        return (
            staging is not None
            and size >= staging.threshold_bytes
            and self._sandbox._transfers.supports_transfers()
        )

    def _plan(self, files: Sequence[WriteEntry]) -> WritePlan:
        """Sin `transfer` no se sondea nada y todo va por gRPC; con él, lo
        grande va por S3 salvo que el agente sea anterior a M9."""
        plan = plan_writes(files, self._sandbox.transfer)
        if plan.routed and not self._sandbox._transfers.supports_transfers():
            return plan.without_routing()
        return plan

    def _require_m9_write(self, *, gzip: bool, metadata: Mapping[str, str]) -> None:
        """Un agente anterior ignoraría en silencio los metadatos y no acepta
        un stream comprimido: la sonda lo detecta antes de mover bytes."""
        if metadata:
            self._sandbox._transfers.require_support("files.write(metadata=)")
        if gzip:
            self._sandbox._transfers.require_support("files.write(gzip=True)")

    def _track(self, handle: WatchHandle) -> None:
        with self._lock:
            self._watches.add(handle)

    def _untrack(self, handle: WatchHandle) -> None:
        with self._lock:
            self._watches.discard(handle)

    def _stop_watches(self) -> None:
        with self._lock:
            handles = list(self._watches)
        for handle in handles:
            handle.stop()


class WatchHandle:
    """Un `watch_dir` vivo. `stop()` cancela el stream, espera al hilo (≤ 5 s)
    y es idempotente; `Sandbox.close()` lo llama por cada handle vivo. Tras
    un suspend/resume (o un corte del proxy) el hilo consumidor espera al
    agente y vuelve a emitir `WatchDir` sin llamar a `on_exit`; `reconnects`
    cuenta esas veces."""

    def __init__(
        self,
        *,
        filesystem: Filesystem,
        call: Any,
        path: str,
        state: WatchState,
        on_exit: ExitCallback | None,
        request: filesystem_pb2.WatchDirRequest | None = None,
        deadline_at: float | None = None,
    ) -> None:
        self._filesystem = filesystem
        self._call = call
        self._path = path
        self._state = state
        self._on_exit = on_exit
        self._request = request
        self._deadline_at = deadline_at
        self._generation = filesystem._sandbox.resume_generation
        self._reconnects = 0
        self._budget = ReconnectBudget()
        self._pending_cut: grpc.RpcError | None = None
        self._thread = threading.Thread(target=self._consume, name=next_watch_name(), daemon=True)

    @property
    def path(self) -> str:
        return self._path

    @property
    def is_running(self) -> bool:
        """False tras `stop()` o cuando el stream terminó por su cuenta."""
        return self._state.is_running

    @property
    def reconnects(self) -> int:
        return self._reconnects

    def get_new_events(self) -> list[FilesystemEvent]:
        """Eventos no entregados desde la última llamada. Si el stream terminó
        con error lo levanta una sola vez; tras un `stop()` limpio devuelve
        listas vacías."""
        return self._state.drain()

    def stop(self) -> None:
        if self._state.stopped:
            return
        self._state.stopped = True
        self._call.cancel()
        if threading.current_thread() is not self._thread:
            self._thread.join(WATCH_STOP_JOIN_SECONDS)
        self._filesystem._untrack(self)

    def _start(self) -> None:
        self._thread.start()

    def _consume(self) -> None:
        failure: Exception | None = None
        try:
            while self._pump_until_cut() and self._reissue():
                pass
        except grpc.RpcError as exc:
            failure = self._classify(exc)
        except Exception as exc:
            failure = exc
        finally:
            self._finish(failure)

    def _pump_until_cut(self) -> bool:
        """Consume el stream actual. True si terminó por un corte reconectable
        con el watch vivo (hay que reabrirlo); False si terminó limpiamente."""
        try:
            for response in self._call:
                self._state.feed(response)
        except grpc.RpcError as exc:
            if self._state.stopped or not self._sandbox._is_reconnectable(exc):
                raise
            self._pending_cut = exc
            return True
        return False

    def _reissue(self) -> bool:
        """Espera al agente y vuelve a emitir el mismo `WatchDir`; si no
        vuelve, la excepción del sondeo cierra el watch como en M3. False si
        alguien llamó a `stop()` mientras tanto (final limpio). Un `WatchDir`
        rechazado por el phase gate (`UNAVAILABLE suspending`: el agente vive
        con el gate aún cerrado tras un `/suspend` que nadie checkpointeó) se
        reintenta con el backoff de `ReconnectPoll` dentro de
        `reconnect_timeout`."""
        reason = self._pending_cut
        if reason is None or self._request is None:
            raise SandboxException("el watch no puede reabrirse sin su request original")
        outcome = self._sandbox._reconnect(reason, self._generation, wake=False)
        if not outcome.resumed:
            raise self._sandbox._reconnect_error(outcome, reason) from reason
        self._generation = outcome.resume_generation
        if self._state.stopped:
            return False
        if not self._budget.allows(outcome):
            raise reason
        self._call = self._reopen_through_the_gate(self._request)
        self._reconnects += 1
        self._cancel_if_stopped()
        return True

    def _reopen_through_the_gate(self, request: filesystem_pb2.WatchDirRequest) -> Any:
        retry = GateRetry(self._sandbox._reconnect_timeout)
        while True:
            try:
                return self._filesystem._open_watch(request, self._remaining_deadline())
            except SandboxException as exc:
                delay = retry.retry_delay(exc)
                if delay is None:
                    raise
                self._sandbox._logger_or(logger).info(
                    "watch %s: el gate del agente sigue cerrado (%s); reintento", self._path, exc
                )
                time.sleep(delay)

    def _cancel_if_stopped(self) -> None:
        """Un `stop()` que llegó mientras se reabría el watch cancela el
        stream nuevo; el bucle lo verá como el final limpio de M3."""
        if self._state.stopped:
            self._call.cancel()

    def _remaining_deadline(self) -> float | None:
        remaining = remaining_deadline(self._deadline_at, time.monotonic)
        if remaining is not None and remaining <= 0.0:
            raise TimeoutException(
                f"el deadline del watch de {self._path} venció durante la reconexión"
            )
        return remaining

    @property
    def _sandbox(self) -> Sandbox:
        return self._filesystem._sandbox

    def _classify(self, exc: grpc.RpcError) -> Exception | None:
        """Un reset con el watch vivo se clasifica sondeando `Health` como en
        M2 (sólo cuando la reconexión ya falló o el corte no es reconectable);
        un `CANCELLED` tras `stop()` es el final limpio."""
        if not self._state.stopped and is_stream_reset(exc):
            return self._filesystem._stream_failure(exc)
        return watch_failure(exc, stopped=self._state.stopped)

    def _finish(self, failure: Exception | None) -> None:
        """Cancela el RPC antes de dar el watch por terminado: si el consumidor
        muere por un mensaje inválido, el stream seguiría abierto y `rayd`
        retendría el inotify y una de sus 64 plazas hasta cerrar el canal."""
        self._call.cancel()
        self._state.record_end(failure)
        self._filesystem._untrack(self)
        notify_exit(self._on_exit, failure)

    def __enter__(self) -> WatchHandle:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.stop()

    def __repr__(self) -> str:
        return f"WatchHandle(path={self._path!r}, is_running={self.is_running})"
