"""`AsyncSandboxPool`: el `SandboxPool` sobre asyncio (misma semántica,
misma custodia del secreto; ver `sandbox_sync.pool`).

El relleno es una `asyncio.Task` que lanza calentamientos acotados por un
`asyncio.Semaphore(fill_concurrency)`; cada calentamiento es
`AsyncSandbox.create()` + `await pause(wait=True)` + `await close()`. Las
llamadas al plano de control van por `asyncio.to_thread` (como en
`AsyncSandbox`) y las de un backend persistente también; el estado en
memoria se protege con un `threading.Lock` porque el aviso de `run-microvm`
llega desde el hilo de `to_thread`.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import logging
import random
import threading
import time
from collections.abc import Awaitable, Callable
from datetime import datetime
from types import TracebackType
from typing import Any, Final, Self

import boto3

from rayito._aws import ControlPlane, PortSpec
from rayito._limits import DEFAULT_PORT, TERMINAL_STATES
from rayito._models import SandboxInfo
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
from rayito.sandbox_async.main import AsyncSandbox
from rayito.sandbox_sync.main import resolve_control_plane, terminate_quietly
from rayito.sandbox_sync.pool import (
    SETTLE_ATTEMPTS,
    SETTLE_CELL,
    SETTLE_GRACE_SECONDS,
    LaunchObserver,
    PoolStoppingError,
    elapsed_ms,
    next_pool_number,
    parked_age_seconds,
    utc_now,
)

logger = logging.getLogger("rayito.pool")

CLOSE_JOIN_SECONDS: Final = 30.0
AsyncSleeper = Callable[[float], Awaitable[None]]


class AsyncSandboxPool:
    """Pool de sandboxes suspendidos para asyncio: `await start()` (o
    `async with`), `await take()`, `await close()`; `stats()` es síncrono."""

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
        sleep: AsyncSleeper | None = None,
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
        self._state_lock = threading.Lock()
        self._records: dict[str, SlotRecord] = {}
        self._counters = PoolCounters()
        self._inflight = 0
        self._pending_backoff: float | None = None
        self._sweep_requested = False
        self._started = False
        self._closed = False
        self._cond: asyncio.Condition | None = None
        self._stopping: asyncio.Event | None = None
        self._semaphore: asyncio.Semaphore | None = None
        self._filler: asyncio.Task[None] | None = None
        self._warm_ups: set[asyncio.Task[None]] = set()
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

    async def start(self) -> Self:
        if self._closed:
            raise pool_closed_error()
        if self._started:
            return self
        self._cond = asyncio.Condition()
        self._stopping = asyncio.Event()
        self._semaphore = asyncio.Semaphore(self._config.fill_concurrency)
        self._image_arn = await asyncio.to_thread(
            self._plane.resolve_template_arn, resolve_template(self._config.template)
        )
        await self._recover()
        await self._sweep()
        self._started = True
        self._filler = asyncio.get_running_loop().create_task(
            self._run_filler(), name=f"rayito-pool-filler-{next_pool_number()}"
        )
        return self

    async def close(self, *, drain: bool = True) -> None:
        if not drain and not self._backend.persistent:
            raise drain_requires_persistence_error()
        if self._closed:
            return
        self._closed = True
        if self._stopping is not None:
            self._stopping.set()
        await self._notify_all()
        await self._join_filler()
        if self._warm_ups:
            await asyncio.gather(*self._warm_ups, return_exceptions=True)
        if drain:
            await self._drain()

    async def _join_filler(self) -> None:
        filler = self._filler
        if filler is None:
            return
        try:
            await asyncio.wait_for(asyncio.shield(filler), CLOSE_JOIN_SECONDS)
        except TimeoutError:
            filler.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await filler

    async def __aenter__(self) -> Self:
        return await self.start()

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        await self.close(drain=True)

    # -------------------------------------------------------------------- take

    async def take(
        self,
        *,
        wait: float = 0.0,
        ready_timeout: float = DEFAULT_READY_TIMEOUT_SECONDS,
        request_timeout: float = DEFAULT_REQUEST_TIMEOUT_SECONDS,
        reconnect_timeout: float = DEFAULT_RECONNECT_TIMEOUT_SECONDS,
    ) -> AsyncSandbox:
        """Misma semántica que `SandboxPool.take`."""
        record = await self._claim_ready_slot(wait)
        sandbox = (
            None
            if record is None
            else await self._open_slot(record, ready_timeout, request_timeout, reconnect_timeout)
        )
        if sandbox is None:
            return await self._fallback(ready_timeout, request_timeout, reconnect_timeout)
        return sandbox

    def stats(self) -> PoolStats:
        with self._state_lock:
            return stats_from_records(
                self._records.values(),
                size=self._config.size,
                warming=self._inflight,
                counters=dataclasses.replace(self._counters),
            )

    # ------------------------------------------------------------ take internals

    async def _claim_ready_slot(self, wait: float) -> SlotRecord | None:
        self._require_open()
        cond = self._condition()
        deadline = self._monotonic() + wait
        async with cond:
            record = await self._pop_ready()
            while record is None and wait > 0:
                remaining = deadline - self._monotonic()
                if remaining <= 0 or not await wait_notified(cond, remaining):
                    break
                self._require_open()
                record = await self._pop_ready()
            cond.notify_all()
            return record

    def _require_open(self) -> None:
        if self._closed:
            raise pool_closed_error()
        if not self._started:
            raise pool_not_started_error()

    def _condition(self) -> asyncio.Condition:
        if self._cond is None:
            raise pool_not_started_error()
        return self._cond

    async def _pop_ready(self) -> SlotRecord | None:
        with self._state_lock:
            record = pick_ready_slot(
                self._records.values(), self._now(), self._config.min_remaining_seconds
            )
            if record is not None:
                del self._records[record.sandbox_id]
        if record is not None:
            await self._backend_call(self._backend.delete, record.sandbox_id)
        return record

    async def _open_slot(
        self,
        record: SlotRecord,
        ready_timeout: float,
        request_timeout: float,
        reconnect_timeout: float,
    ) -> AsyncSandbox | None:
        started = self._monotonic()
        if not await self._resume_slot(record):
            return None
        resumed_at = self._monotonic()
        try:
            sandbox = await AsyncSandbox._open(
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

    async def _resume_slot(self, record: SlotRecord) -> bool:
        try:
            resumed = await asyncio.to_thread(self._plane.resume_microvm, record.sandbox_id)
        except (SandboxNotFoundException, SandboxStateException):
            resumed = False
        except Exception as exc:
            self._count(failed=1)
            logger.warning(
                "plaza %s: resume-microvm falló (%s); fallback a create()",
                record.sandbox_id,
                type(exc).__name__,
            )
            await self._terminate(record.sandbox_id)
            return False
        if not resumed:
            self._count(lost=1)
            logger.warning("plaza %s perdida al tomarla: ya no estaba SUSPENDED", record.sandbox_id)
            await self._terminate(record.sandbox_id)
        return resumed

    async def _fallback(
        self, ready_timeout: float, request_timeout: float, reconnect_timeout: float
    ) -> AsyncSandbox:
        plane = LaunchObserver(self._plane, lambda _info: self._count(launched=1))
        try:
            sandbox = await AsyncSandbox.create(
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

    async def _run_filler(self) -> None:
        stopping = self._stopping_event()
        while not stopping.is_set():
            await self._fill()
            await self._wait_for_work()
            if not stopping.is_set() and self._sweep_due():
                await self._sweep()

    def _stopping_event(self) -> asyncio.Event:
        if self._stopping is None:
            raise pool_not_started_error()
        return self._stopping

    async def _fill(self) -> None:
        delay = self._take_pending_backoff()
        if delay is not None:
            await self._pause(delay)
        if self._stopping_event().is_set():
            return
        self._submit_warm_ups()

    def _take_pending_backoff(self) -> float | None:
        with self._state_lock:
            delay = self._pending_backoff
            self._pending_backoff = None
            return delay

    async def _pause(self, delay: float) -> None:
        if self._sleep is not None:
            await self._sleep(delay)
            return
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self._stopping_event().wait(), delay)

    def _submit_warm_ups(self) -> None:
        with self._state_lock:
            deficit = self._deficit_locked()
            self._inflight += max(0, deficit)
        loop = asyncio.get_running_loop()
        for _ in range(deficit):
            task = loop.create_task(self._warm_up())
            self._warm_ups.add(task)
            task.add_done_callback(self._warm_ups.discard)

    def _deficit_locked(self) -> int:
        ready = sum(1 for record in self._records.values() if record.state == "ready")
        return self._config.size - ready - self._inflight

    async def _wait_for_work(self) -> None:
        cond = self._condition()
        async with cond:
            if self._stopping_event().is_set() or self._pending_backoff is not None:
                return
            with self._state_lock:
                deficit = self._deficit_locked()
            if deficit > 0 or self._sweep_due():
                return
            await wait_notified(cond, self._seconds_until_sweep())

    def _sweep_due(self) -> bool:
        return self._sweep_requested or self._seconds_until_sweep() <= 0

    def _seconds_until_sweep(self) -> float:
        elapsed = self._monotonic() - self._last_sweep
        return max(0.0, self._config.sweep_interval_seconds - elapsed)

    async def _request_sweep(self) -> None:
        """Adelanta el siguiente barrido (tests)."""
        self._sweep_requested = True
        await self._notify_all()

    async def _notify_all(self) -> None:
        cond = self._cond
        if cond is None:
            return
        async with cond:
            cond.notify_all()

    # ------------------------------------------------------------------ warm-up

    async def _warm_up(self) -> None:
        semaphore = self._semaphore
        try:
            if semaphore is None:
                return
            async with semaphore:
                await self._warm_up_once()
        finally:
            with self._state_lock:
                self._inflight -= 1
            await self._notify_all()

    async def _warm_up_once(self) -> None:
        token = generate_access_token()
        started = self._monotonic()
        launched: list[str] = []

        def on_launch(info: SandboxInfo) -> None:
            launched.append(info.sandbox_id)
            self._record_launch(info, token)

        try:
            sandbox = await AsyncSandbox.create(
                **self._launch_kwargs,
                access_token=token,
                keep_on_failure=False,
                control_plane=LaunchObserver(self._plane, on_launch),
                transport=self._transport,
            )
        except Exception as exc:
            await self._warm_up_failed(exc, launched[0] if launched else None)
            return
        logger.info(
            "plaza %s caliente en %d ms",
            sandbox.sandbox_id,
            elapsed_ms(self._monotonic(), started),
        )
        await self._park(sandbox, token)

    async def _park(self, sandbox: AsyncSandbox, token: str) -> None:
        started = self._monotonic()
        stopping = self._stopping_event()
        try:
            await self._settle(sandbox)
            if stopping.is_set():
                raise PoolStoppingError()
            await sandbox.pause(wait=True)
            if stopping.is_set():
                raise PoolStoppingError()
        except PoolStoppingError:
            await self._discard(sandbox.sandbox_id)
            return
        except Exception as exc:
            await self._warm_up_failed(exc, sandbox.sandbox_id)
            await self._terminate(sandbox.sandbox_id)
            return
        finally:
            await sandbox.close()
        record = record_from_info(
            sandbox.info,
            access_token=token,
            region=self._plane.region,
            state="ready",
            parked_at=self._now(),
        )
        await self._record_ready(record)
        logger.info(
            "plaza %s aparcada en %d ms", record.sandbox_id, elapsed_ms(self._monotonic(), started)
        )

    async def _settle(self, sandbox: AsyncSandbox) -> None:
        """Misma regla que `SandboxPool._settle`: una celda trivial y un
        `Health` tras una pausa breve antes de aparcar."""
        for _ in range(SETTLE_ATTEMPTS):
            await sandbox.run_code(SETTLE_CELL, request_timeout=self._config.ready_timeout)
            await self._settle_grace()
            if (await sandbox.get_health()).kernel_ready:
                return
            await sandbox._wait_until_ready(terminate_on_failure=False)
        logger.warning("plaza %s: el kernel siguió rotando tras asentarla", sandbox.sandbox_id)

    async def _settle_grace(self) -> None:
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self._stopping_event().wait(), SETTLE_GRACE_SECONDS)

    def _record_launch(self, info: SandboxInfo, token: str) -> None:
        """Corre en el hilo de `to_thread` de `run_microvm`, antes de que
        `create()` vea el VM: sólo estado bajo el lock de hilos y el backend en
        ese mismo hilo. Si el backend falla aquí, `create()` no puede terminar
        el VM, así que se termina en este punto y se propaga el error."""
        record = record_from_info(
            info, access_token=token, region=self._plane.region, state="warming"
        )
        try:
            with self._state_lock:
                self._backend.save(record)
                self._records[record.sandbox_id] = record
                self._counters.launched += 1
        except Exception:
            terminate_quietly(self._plane, info.sandbox_id)
            raise

    async def _record_ready(self, record: SlotRecord) -> None:
        with self._state_lock:
            self._records[record.sandbox_id] = record
            self._backoff.reset()
        await self._backend_call(self._backend.save, record)
        await self._notify_all()

    async def _warm_up_failed(self, exc: Exception, sandbox_id: str | None) -> None:
        with self._state_lock:
            forgotten = sandbox_id is not None and self._records.pop(sandbox_id, None) is not None
            self._counters.failed += 1
            delay = self._backoff.next_delay()
            self._pending_backoff = delay
        if forgotten and sandbox_id is not None:
            await self._backend_call(self._backend.delete, sandbox_id)
        await self._notify_all()
        logger.warning(
            "calentamiento fallido (%s, plaza %s); siguiente intento en %.1f s",
            type(exc).__name__,
            sandbox_id or "sin lanzar",
            delay,
        )

    async def _discard(self, sandbox_id: str) -> None:
        await self._forget(sandbox_id)
        await self._terminate(sandbox_id)

    # ------------------------------------------------------------------- sweep

    async def _sweep(self) -> None:
        self._sweep_requested = False
        self._last_sweep = self._monotonic()
        await self._recycle_stale()
        await self._reconcile()

    async def _recycle_stale(self) -> None:
        now = self._now()
        for record in self._ready_records():
            if slot_is_stale(
                record, now, self._config.min_remaining_seconds
            ) and await self._forget(record.sandbox_id):
                self._count(recycled=1)
                logger.warning(
                    "plaza %s reciclada: le quedaban %.0f s (< %d)",
                    record.sandbox_id,
                    record.remaining_seconds(now),
                    self._config.min_remaining_seconds,
                )
                await self._terminate(record.sandbox_id)

    async def _reconcile(self) -> None:
        try:
            listed = await asyncio.to_thread(self._listed_states)
        except Exception as exc:
            logger.warning("barrido abortado: list-microvms falló (%s)", type(exc).__name__)
            return
        for record in self._ready_records():
            action = reconcile_action(record, listed.get(record.sandbox_id))
            if action == "repark":
                await self._repark(record)
            elif action == "check":
                await self._check(record)
            elif action == "drop":
                await self._lose(record, listed.get(record.sandbox_id) or "terminal")

    def _listed_states(self) -> dict[str, str]:
        return {
            item.sandbox_id: item.state
            for item in self._plane.list_microvms(
                image_arn=self._image_arn, image_version=self._config.template_version
            )
        }

    async def _repark(self, record: SlotRecord) -> None:
        try:
            await asyncio.to_thread(self._plane.suspend_microvm, record.sandbox_id)
        except Exception as exc:
            logger.warning(
                "plaza %s: no se pudo volver a aparcar (%s)", record.sandbox_id, type(exc).__name__
            )
            return
        logger.warning("plaza %s reanudada fuera del pool; vuelta a aparcar", record.sandbox_id)

    async def _check(self, record: SlotRecord) -> None:
        try:
            info = await asyncio.to_thread(self._plane.get_microvm, record.sandbox_id)
        except SandboxNotFoundException:
            await self._lose(record, "not found")
            return
        except Exception as exc:
            logger.warning(
                "plaza %s: get-microvm falló en el barrido (%s)",
                record.sandbox_id,
                type(exc).__name__,
            )
            return
        if info.state in TERMINAL_STATES:
            await self._lose(record, f"{info.state}: {info.state_reason or 'sin stateReason'}")

    async def _lose(self, record: SlotRecord, reason: str) -> None:
        if await self._forget(record.sandbox_id):
            self._count(lost=1)
            logger.warning("plaza %s perdida fuera del pool (%s)", record.sandbox_id, reason)

    # ---------------------------------------------------------------- recovery

    async def _recover(self) -> None:
        for record in await self._backend_call(self._backend.load):
            if record.state == "warming":
                await self._backend_call(self._backend.delete, record.sandbox_id)
                self._count(lost=1)
                logger.warning(
                    "plaza %s huérfana de un calentamiento interrumpido; terminada",
                    record.sandbox_id,
                )
                await self._terminate(record.sandbox_id)
            else:
                with self._state_lock:
                    self._records[record.sandbox_id] = record

    async def _drain(self) -> None:
        for record in self._all_records():
            if await self._forget(record.sandbox_id):
                await self._terminate(record.sandbox_id)

    # ----------------------------------------------------------------- helpers

    def _ready_records(self) -> list[SlotRecord]:
        with self._state_lock:
            return [record for record in self._records.values() if record.state == "ready"]

    def _all_records(self) -> list[SlotRecord]:
        with self._state_lock:
            return list(self._records.values())

    async def _forget(self, sandbox_id: str) -> bool:
        with self._state_lock:
            forgotten = self._records.pop(sandbox_id, None) is not None
        if forgotten:
            await self._backend_call(self._backend.delete, sandbox_id)
            await self._notify_all()
        return forgotten

    async def _terminate(self, sandbox_id: str) -> None:
        await asyncio.to_thread(terminate_quietly, self._plane, sandbox_id)

    async def _backend_call(self, call: Callable[..., Any], *args: Any) -> Any:
        if self._backend.persistent:
            return await asyncio.to_thread(self._locked_backend_call, call, *args)
        return self._locked_backend_call(call, *args)

    def _locked_backend_call(self, call: Callable[..., Any], *args: Any) -> Any:
        with self._state_lock:
            return call(*args)

    def _count(self, **deltas: int) -> None:
        with self._state_lock:
            for name, delta in deltas.items():
                setattr(self._counters, name, getattr(self._counters, name) + delta)


async def wait_notified(cond: asyncio.Condition, timeout: float) -> bool:
    """`cond.wait()` con plazo: `False` si venció sin aviso. El caller tiene el lock."""
    try:
        await asyncio.wait_for(cond.wait(), timeout)
    except TimeoutError:
        return False
    return True


__all__ = ["AsyncSandboxPool"]
