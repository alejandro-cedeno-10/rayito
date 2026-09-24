"""`SandboxPool`: N MicroVMs aparcados (`SUSPENDED`) que `take()` entrega en
menos de un segundo (ADR-008, `m7-suspended-pool`).

Un hilo de relleno mantiene `ready + warming == size`: cada plaza se calienta
con el `create()` normal (token fresco por plaza, `agent_ready` y
`kernel_ready`), se aparca con `pause(wait=True)` y se cierra el handle: el
pool guarda **datos** (`SlotRecord` en un `PoolBackend`), nunca canales ni
JWE. `take()` saca la plaza más vieja con vida suficiente, borra su registro
antes de tocar la red, hace `resume-microvm` explícito y abre el handle con
el token de la plaza; si no hay plaza (o la plaza murió) cae a un `create()`
normal: el pool es una optimización de latencia, nunca un semáforo. Un
sweeper recicla las plazas que se acercan al muro de 8 h y reconcilia con
`list-microvms`. Todas las llamadas a AWS pasan por el plano de control
compartido, así que comparten los token buckets con el resto del proceso.

Custodia del secreto (SECURITY.md T14): el sha256 del token viaja en el
`runHookPayload` y `/run` se acepta una vez por arranque, así que el token
que abre una plaza es el que acuñó el pool durante toda la vida del VM; no
hay rotación en `take()`. Un pool que nunca se cierra deja `size` VMs
suspendidos que se terminan solos en su `timeout` (política de idle).
"""

from __future__ import annotations

import dataclasses
import logging
import random
import threading
import time
from collections.abc import Callable, Iterable, Iterator, Sequence
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from types import TracebackType
from typing import Final, Self

import boto3

from rayito._aws import ControlPlane, LaunchRequest, PortSpec
from rayito._limits import DEFAULT_PORT, TERMINAL_STATES
from rayito._models import MicrovmListPage, SandboxInfo, SandboxListItem
from rayito._payload import generate_access_token
from rayito._pool_backends import InMemoryPoolBackend, PoolBackend
from rayito._pool_base import (
    FillBackoff,
    PoolConfig,
    PoolCounters,
    PoolStats,
    SlotRecord,
    TakePoll,
    drain_requires_persistence_error,
    launch_kwargs,
    pick_ready_slot,
    pool_closed_error,
    pool_not_started_error,
    reconcile_action,
    record_from_info,
    sandbox_info_from_record,
    slot_is_stale,
    stats_from_records,
)
from rayito._sandbox_base import (
    DEFAULT_READY_TIMEOUT_SECONDS,
    DEFAULT_RECONNECT_TIMEOUT_SECONDS,
    DEFAULT_REQUEST_TIMEOUT_SECONDS,
    resolve_template,
)
from rayito._transport import TransportSettings
from rayito.exceptions import SandboxNotFoundException, SandboxStateException
from rayito.sandbox_sync.main import Sandbox, resolve_control_plane, terminate_quietly

logger = logging.getLogger("rayito.pool")

CLOSE_JOIN_SECONDS: Final = 30.0
SETTLE_CELL: Final = "pass"
SETTLE_GRACE_SECONDS: Final = 0.3
SETTLE_ATTEMPTS: Final = 3
_pool_sequence = 0
_pool_sequence_lock = threading.Lock()


def next_pool_number() -> int:
    global _pool_sequence
    with _pool_sequence_lock:
        _pool_sequence += 1
        return _pool_sequence


def utc_now() -> datetime:
    return datetime.now(UTC)


class LaunchObserver:
    """Un `ControlPlane` que delega todo en otro y avisa con la `SandboxInfo`
    de cada `run-microvm` aceptado: así el pool guarda el registro `warming`
    en el instante en que existe el VM, sin desmontar `create()`."""

    def __init__(self, plane: ControlPlane, on_launch: Callable[[SandboxInfo], None]) -> None:
        self._plane = plane
        self._on_launch = on_launch

    @property
    def region(self) -> str:
        return self._plane.region

    def resolve_template_arn(self, template: str) -> str:
        return self._plane.resolve_template_arn(template)

    def run_microvm(self, request: LaunchRequest) -> SandboxInfo:
        info = self._plane.run_microvm(request)
        self._on_launch(info)
        return info

    def get_microvm(self, sandbox_id: str) -> SandboxInfo:
        return self._plane.get_microvm(sandbox_id)

    def list_microvms(
        self,
        *,
        image_arn: str | None = None,
        image_version: str | None = None,
        states: Iterable[str] | None = None,
    ) -> Iterator[SandboxListItem]:
        return self._plane.list_microvms(
            image_arn=image_arn, image_version=image_version, states=states
        )

    def list_microvms_page(
        self,
        *,
        image_arn: str | None,
        image_version: str | None,
        max_results: int,
        next_token: str | None,
    ) -> MicrovmListPage:
        return self._plane.list_microvms_page(
            image_arn=image_arn,
            image_version=image_version,
            max_results=max_results,
            next_token=next_token,
        )

    def terminate_microvm(self, sandbox_id: str) -> bool:
        return self._plane.terminate_microvm(sandbox_id)

    def suspend_microvm(self, sandbox_id: str) -> bool:
        return self._plane.suspend_microvm(sandbox_id)

    def resume_microvm(self, sandbox_id: str) -> bool:
        return self._plane.resume_microvm(sandbox_id)

    def create_auth_token(self, sandbox_id: str, ports: Sequence[PortSpec]) -> str:
        return self._plane.create_auth_token(sandbox_id, ports)


class SandboxPool:
    """Pool de sandboxes suspendidos. Arrancar con `start()` (o `with`),
    tomar con `take()`, cerrar con `close()`.

    `sleep` es la espera del backoff del relleno; por defecto es
    interrumpible por `close()`. `monotonic`, `now` y `random` son
    inyectables para los tests.
    """

    def __init__(
        self,
        config: PoolConfig,
        *,
        backend: PoolBackend | None = None,
        region: str | None = None,
        session: boto3.session.Session | None = None,
        control_plane: ControlPlane | None = None,
        transport: TransportSettings | None = None,
        monotonic: Callable[[], float] = time.monotonic,
        now: Callable[[], datetime] = utc_now,
        sleep: Callable[[float], None] | None = None,
        random: Callable[[], float] = random.random,
    ) -> None:
        self._config = config
        self._backend: PoolBackend = backend if backend is not None else InMemoryPoolBackend()
        self._plane = resolve_control_plane(control_plane, session, region)
        self._session = session
        self._transport = transport or TransportSettings()
        self._monotonic = monotonic
        self._now = now
        self._sleep = sleep
        self._backoff = FillBackoff(random=random)
        self._launch_kwargs = launch_kwargs(config)
        self._lock = threading.Lock()
        self._cond = threading.Condition(self._lock)
        self._records: dict[str, SlotRecord] = {}
        self._counters = PoolCounters()
        self._inflight = 0
        self._pending_backoff: float | None = None
        self._sweep_requested = False
        self._started = False
        self._closed = False
        self._stopping = threading.Event()
        self._filler: threading.Thread | None = None
        self._executor: ThreadPoolExecutor | None = None
        self._image_arn: str | None = None
        self._last_sweep = monotonic()

    @property
    def config(self) -> PoolConfig:
        return self._config

    @property
    def session(self) -> boto3.session.Session | None:
        """La sesión boto3 del pool: con ella firman sus plazas las URLs y
        las transferencias enrutadas de `transfer=S3Staging(...)`."""
        return self._session

    @property
    def backend(self) -> PoolBackend:
        return self._backend

    # --------------------------------------------------------------- lifecycle

    def start(self) -> Self:
        """Resuelve la imagen, recupera el backend, barre y arranca el hilo de
        relleno. Idempotente."""
        with self._cond:
            if self._closed:
                raise pool_closed_error()
            if self._started:
                return self
        self._image_arn = self._plane.resolve_template_arn(resolve_template(self._config.template))
        self._recover()
        self._sweep()
        self._executor = ThreadPoolExecutor(
            max_workers=self._config.fill_concurrency, thread_name_prefix="rayito-pool-warmup"
        )
        self._filler = threading.Thread(
            target=self._run_filler, name=f"rayito-pool-filler-{next_pool_number()}", daemon=True
        )
        with self._cond:
            self._started = True
        self._filler.start()
        return self

    def close(self, *, drain: bool = True) -> None:
        """Para el relleno y, con `drain`, termina todas las plazas `ready`.
        `drain=False` sólo con un backend persistente: deja las plazas
        aparcadas para que otro pool las recupere. Idempotente."""
        if not drain and not self._backend.persistent:
            raise drain_requires_persistence_error()
        with self._cond:
            if self._closed:
                return
            self._closed = True
            self._stopping.set()
            self._cond.notify_all()
        if self._filler is not None:
            self._filler.join(timeout=CLOSE_JOIN_SECONDS)
        if self._executor is not None:
            self._executor.shutdown(wait=True)
        if drain:
            self._drain()

    def __enter__(self) -> Self:
        return self.start()

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close(drain=True)

    # -------------------------------------------------------------------- take

    def take(
        self,
        *,
        wait: float = 0.0,
        ready_timeout: float = DEFAULT_READY_TIMEOUT_SECONDS,
        request_timeout: float = DEFAULT_REQUEST_TIMEOUT_SECONDS,
        reconnect_timeout: float = DEFAULT_RECONNECT_TIMEOUT_SECONDS,
    ) -> Sandbox:
        """La plaza `ready` más vieja con al menos `min_remaining_seconds` de
        vida, reanudada y abierta con su token; sin plaza (tras esperar hasta
        `wait` s) o con una plaza muerta, un `create()` normal."""
        record = self._claim_ready_slot(wait)
        sandbox = (
            None
            if record is None
            else self._open_slot(record, ready_timeout, request_timeout, reconnect_timeout)
        )
        if sandbox is None:
            return self._fallback(ready_timeout, request_timeout, reconnect_timeout)
        return sandbox

    def stats(self) -> PoolStats:
        with self._lock:
            return stats_from_records(
                self._records.values(),
                size=self._config.size,
                warming=self._inflight,
                counters=dataclasses.replace(self._counters),
            )

    # ------------------------------------------------------------ take internals

    def _claim_ready_slot(self, wait: float) -> SlotRecord | None:
        deadline = self._monotonic() + wait
        with self._cond:
            self._require_open()
            record = self._pop_ready()
            while record is None and wait > 0:
                remaining = deadline - self._monotonic()
                if remaining <= 0 or not self._cond.wait(remaining):
                    break
                self._require_open()
                record = self._pop_ready()
            self._cond.notify_all()
            return record

    def _require_open(self) -> None:
        if self._closed:
            raise pool_closed_error()
        if not self._started:
            raise pool_not_started_error()

    def _pop_ready(self) -> SlotRecord | None:
        record = pick_ready_slot(
            self._records.values(), self._now(), self._config.min_remaining_seconds
        )
        if record is not None:
            del self._records[record.sandbox_id]
            self._backend.delete(record.sandbox_id)
        return record

    def _open_slot(
        self,
        record: SlotRecord,
        ready_timeout: float,
        request_timeout: float,
        reconnect_timeout: float,
    ) -> Sandbox | None:
        started = self._monotonic()
        if not self._resume_slot(record):
            return None
        resumed_at = self._monotonic()
        try:
            sandbox = Sandbox._open(
                sandbox_info_from_record(record),
                access_token=record.access_token,
                control_plane=self._plane,
                transport=self._transport,
                proxy_ports=(PortSpec.single(DEFAULT_PORT),),
                request_timeout=request_timeout,
                ready_timeout=ready_timeout,
                reconnect_timeout=reconnect_timeout,
                terminate_on_failure=True,
                readiness=TakePoll,
            )
        except Exception as exc:
            self._count(failed=1)
            logger.warning(
                "plaza %s: no se pudo abrir al tomarla (%s); fallback a create()",
                record.sandbox_id,
                type(exc).__name__,
            )
            return None
        self._count(takes=1, hits=1)
        now = self._monotonic()
        logger.info(
            "plaza %s tomada en %d ms (resume-microvm %d ms, apertura %d ms, aparcada hace %.0f s)",
            record.sandbox_id,
            elapsed_ms(now, started),
            elapsed_ms(resumed_at, started),
            elapsed_ms(now, resumed_at),
            parked_age_seconds(record, self._now()),
        )
        return sandbox

    def _resume_slot(self, record: SlotRecord) -> bool:
        try:
            resumed = self._plane.resume_microvm(record.sandbox_id)
        except (SandboxNotFoundException, SandboxStateException):
            resumed = False
        except Exception as exc:
            self._count(failed=1)
            logger.warning(
                "plaza %s: resume-microvm falló (%s); fallback a create()",
                record.sandbox_id,
                type(exc).__name__,
            )
            terminate_quietly(self._plane, record.sandbox_id)
            return False
        if not resumed:
            self._count(lost=1)
            logger.warning("plaza %s perdida al tomarla: ya no estaba SUSPENDED", record.sandbox_id)
            terminate_quietly(self._plane, record.sandbox_id)
        return resumed

    def _fallback(
        self, ready_timeout: float, request_timeout: float, reconnect_timeout: float
    ) -> Sandbox:
        plane = LaunchObserver(self._plane, lambda _info: self._count(launched=1))
        try:
            sandbox = Sandbox.create(
                **{**self._launch_kwargs, "ready_timeout": ready_timeout},
                access_token=generate_access_token(),
                keep_on_failure=False,
                control_plane=plane,
                transport=self._transport,
                request_timeout=request_timeout,
                reconnect_timeout=reconnect_timeout,
            )
        except Exception:
            self._count(failed=1)
            raise
        self._count(takes=1, misses=1)
        logger.info("pool sin plaza lista: sandbox %s creado con create()", sandbox.sandbox_id)
        return sandbox

    # ------------------------------------------------------------------ filler

    def _run_filler(self) -> None:
        while not self._stopping.is_set():
            self._fill()
            self._wait_for_work()
            if not self._stopping.is_set() and self._sweep_due():
                self._sweep()

    def _fill(self) -> None:
        delay = self._take_pending_backoff()
        if delay is not None:
            self._pause(delay)
        if self._stopping.is_set():
            return
        self._submit_warm_ups()

    def _take_pending_backoff(self) -> float | None:
        with self._lock:
            delay = self._pending_backoff
            self._pending_backoff = None
            return delay

    def _pause(self, delay: float) -> None:
        if self._sleep is None:
            self._stopping.wait(delay)
        else:
            self._sleep(delay)

    def _submit_warm_ups(self) -> None:
        with self._lock:
            deficit = self._deficit_locked()
            self._inflight += max(0, deficit)
        executor = self._executor
        if executor is None:
            return
        for _ in range(deficit):
            executor.submit(self._warm_up)

    def _deficit_locked(self) -> int:
        ready = sum(1 for record in self._records.values() if record.state == "ready")
        return self._config.size - ready - self._inflight

    def _wait_for_work(self) -> None:
        with self._cond:
            if self._stopping.is_set() or self._pending_backoff is not None:
                return
            if self._deficit_locked() > 0 or self._sweep_due():
                return
            self._cond.wait(self._seconds_until_sweep())

    def _sweep_due(self) -> bool:
        return self._sweep_requested or self._seconds_until_sweep() <= 0

    def _seconds_until_sweep(self) -> float:
        elapsed = self._monotonic() - self._last_sweep
        return max(0.0, self._config.sweep_interval_seconds - elapsed)

    def _request_sweep(self) -> None:
        """Adelanta el siguiente barrido (tests)."""
        with self._cond:
            self._sweep_requested = True
            self._cond.notify_all()

    # ------------------------------------------------------------------ warm-up

    def _warm_up(self) -> None:
        try:
            self._warm_up_once()
        finally:
            with self._cond:
                self._inflight -= 1
                self._cond.notify_all()

    def _warm_up_once(self) -> None:
        token = generate_access_token()
        started = self._monotonic()
        launched: list[str] = []

        def on_launch(info: SandboxInfo) -> None:
            launched.append(info.sandbox_id)
            self._record_launch(info, token)

        try:
            sandbox = Sandbox.create(
                **self._launch_kwargs,
                access_token=token,
                keep_on_failure=False,
                control_plane=LaunchObserver(self._plane, on_launch),
                transport=self._transport,
            )
        except Exception as exc:
            self._warm_up_failed(exc, launched[0] if launched else None)
            return
        logger.info(
            "plaza %s caliente en %d ms",
            sandbox.sandbox_id,
            elapsed_ms(self._monotonic(), started),
        )
        self._park(sandbox, token)

    def _park(self, sandbox: Sandbox, token: str) -> None:
        started = self._monotonic()
        try:
            self._settle(sandbox)
            if self._stopping.is_set():
                raise PoolStoppingError()
            sandbox.pause(wait=True)
            if self._stopping.is_set():
                raise PoolStoppingError()
        except PoolStoppingError:
            self._discard(sandbox.sandbox_id)
            return
        except Exception as exc:
            self._warm_up_failed(exc, sandbox.sandbox_id)
            terminate_quietly(self._plane, sandbox.sandbox_id)
            return
        finally:
            sandbox.close()
        record = record_from_info(
            sandbox.info,
            access_token=token,
            region=self._plane.region,
            state="ready",
            parked_at=self._now(),
        )
        self._record_ready(record)
        logger.info(
            "plaza %s aparcada en %d ms", record.sandbox_id, elapsed_ms(self._monotonic(), started)
        )

    def _settle(self, sandbox: Sandbox) -> None:
        """Cierra la ventana entre el 200 de `/run` y el arranque de la
        rotación del kernel: `rayd` la despacha en segundo plano y un
        `Health` que llega antes ve `kernel_ready` del kernel sin rotar (2 de
        40 calentamientos del e2e de M7 vieron `kernel_ready` a los 2-3 s y
        perdieron el kernel al reanudar). Una celda trivial sólo vuelve
        sobre el kernel rotado (`rayd` retiene `Execute` mientras el contexto
        reinicia) y un `Health` tras una pausa breve confirma que no arrancó
        una rotación después."""
        for _ in range(SETTLE_ATTEMPTS):
            sandbox.run_code(SETTLE_CELL, request_timeout=self._config.ready_timeout)
            self._stopping.wait(SETTLE_GRACE_SECONDS)
            if sandbox.get_health().kernel_ready:
                return
            sandbox._wait_until_ready(terminate_on_failure=False)
        logger.warning("plaza %s: el kernel siguió rotando tras asentarla", sandbox.sandbox_id)

    def _record_launch(self, info: SandboxInfo, token: str) -> None:
        """Corre dentro de `run_microvm`, antes de que `create()` vea el VM:
        si el backend falla aquí, `create()` no puede terminarlo, así que se
        termina en este punto y se propaga el error al calentamiento."""
        record = record_from_info(
            info, access_token=token, region=self._plane.region, state="warming"
        )
        try:
            with self._lock:
                self._backend.save(record)
                self._records[record.sandbox_id] = record
                self._counters.launched += 1
        except Exception:
            terminate_quietly(self._plane, info.sandbox_id)
            raise

    def _record_ready(self, record: SlotRecord) -> None:
        with self._cond:
            self._records[record.sandbox_id] = record
            self._backend.save(record)
            self._backoff.reset()
            self._cond.notify_all()

    def _warm_up_failed(self, exc: Exception, sandbox_id: str | None) -> None:
        with self._cond:
            if sandbox_id is not None:
                self._forget_locked(sandbox_id)
            self._counters.failed += 1
            delay = self._backoff.next_delay()
            self._pending_backoff = delay
            self._cond.notify_all()
        logger.warning(
            "calentamiento fallido (%s, plaza %s); siguiente intento en %.1f s",
            type(exc).__name__,
            sandbox_id or "sin lanzar",
            delay,
        )

    def _discard(self, sandbox_id: str) -> None:
        """Un calentamiento que terminó con el pool parándose: se termina el
        VM en vez de aparcarlo."""
        self._forget(sandbox_id)
        terminate_quietly(self._plane, sandbox_id)

    # ------------------------------------------------------------------- sweep

    def _sweep(self) -> None:
        with self._lock:
            self._sweep_requested = False
        self._last_sweep = self._monotonic()
        self._recycle_stale()
        self._reconcile()

    def _recycle_stale(self) -> None:
        now = self._now()
        for record in self._ready_records():
            if slot_is_stale(record, now, self._config.min_remaining_seconds) and self._forget(
                record.sandbox_id
            ):
                self._count(recycled=1)
                logger.warning(
                    "plaza %s reciclada: le quedaban %.0f s (< %d)",
                    record.sandbox_id,
                    record.remaining_seconds(now),
                    self._config.min_remaining_seconds,
                )
                terminate_quietly(self._plane, record.sandbox_id)

    def _reconcile(self) -> None:
        try:
            listed = {
                item.sandbox_id: item.state
                for item in self._plane.list_microvms(
                    image_arn=self._image_arn, image_version=self._config.template_version
                )
            }
        except Exception as exc:
            logger.warning("barrido abortado: list-microvms falló (%s)", type(exc).__name__)
            return
        for record in self._ready_records():
            action = reconcile_action(record, listed.get(record.sandbox_id))
            if action == "repark":
                self._repark(record)
            elif action == "check":
                self._check(record)
            elif action == "drop":
                self._lose(record, listed.get(record.sandbox_id) or "terminal")

    def _repark(self, record: SlotRecord) -> None:
        try:
            self._plane.suspend_microvm(record.sandbox_id)
        except Exception as exc:
            logger.warning(
                "plaza %s: no se pudo volver a aparcar (%s)", record.sandbox_id, type(exc).__name__
            )
            return
        logger.warning("plaza %s reanudada fuera del pool; vuelta a aparcar", record.sandbox_id)

    def _check(self, record: SlotRecord) -> None:
        try:
            info = self._plane.get_microvm(record.sandbox_id)
        except SandboxNotFoundException:
            self._lose(record, "not found")
            return
        except Exception as exc:
            logger.warning(
                "plaza %s: get-microvm falló en el barrido (%s)",
                record.sandbox_id,
                type(exc).__name__,
            )
            return
        if info.state in TERMINAL_STATES:
            self._lose(record, f"{info.state}: {info.state_reason or 'sin stateReason'}")

    def _lose(self, record: SlotRecord, reason: str) -> None:
        if self._forget(record.sandbox_id):
            self._count(lost=1)
            logger.warning("plaza %s perdida fuera del pool (%s)", record.sandbox_id, reason)

    # ---------------------------------------------------------------- recovery

    def _recover(self) -> None:
        for record in self._backend.load():
            if record.state == "warming":
                self._backend.delete(record.sandbox_id)
                self._count(lost=1)
                logger.warning(
                    "plaza %s huérfana de un calentamiento interrumpido; terminada",
                    record.sandbox_id,
                )
                terminate_quietly(self._plane, record.sandbox_id)
            else:
                self._records[record.sandbox_id] = record

    def _drain(self) -> None:
        for record in self._all_records():
            if self._forget(record.sandbox_id):
                terminate_quietly(self._plane, record.sandbox_id)

    # ----------------------------------------------------------------- helpers

    def _ready_records(self) -> list[SlotRecord]:
        with self._lock:
            return [record for record in self._records.values() if record.state == "ready"]

    def _all_records(self) -> list[SlotRecord]:
        with self._lock:
            return list(self._records.values())

    def _forget(self, sandbox_id: str) -> bool:
        with self._cond:
            forgotten = self._forget_locked(sandbox_id)
            if forgotten:
                self._cond.notify_all()
            return forgotten

    def _forget_locked(self, sandbox_id: str) -> bool:
        if self._records.pop(sandbox_id, None) is None:
            return False
        self._backend.delete(sandbox_id)
        return True

    def _count(self, **deltas: int) -> None:
        with self._lock:
            for name, delta in deltas.items():
                setattr(self._counters, name, getattr(self._counters, name) + delta)


class PoolStoppingError(Exception):
    """Señal interna: el pool se está cerrando y el calentamiento no debe aparcar."""


def elapsed_ms(now: float, started: float) -> int:
    return int(max(0.0, now - started) * 1000)


def parked_age_seconds(record: SlotRecord, now: datetime) -> float:
    if record.parked_at is None:
        return 0.0
    return max(0.0, (now - record.parked_at).total_seconds())


__all__ = ["LaunchObserver", "SandboxPool"]
