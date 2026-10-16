"""`AsyncAgentProject.build()`, `run()` y `deploy()`: el espejo asíncrono de
`_project_service.py` (ADR-028, D6), sobre `AsyncAgentTemplate`,
`AsyncSandbox` y `AsyncAgentDeployment`. Mismas reglas: nada cuesta hasta
llamar a una operación y `deploy()` mata su sandbox ante cualquier fallo
(salvo `keep_on_failure`).
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING, Any

from rayito._agent._deployment_async import AsyncAgentDeployment, default_async_transport
from rayito._agent._events import AgentEvent, AgentResult
from rayito._agent._project_base import (
    AGENT_SERVE_TAG,
    AGENT_SERVICE_READY_POLL_SECONDS,
    clear_markers_command,
    deploy_create_args,
    failed_marker_path,
    host_config_for,
    host_entries,
    is_raw_app,
    is_raw_project,
    kept_sandbox_note,
    project_template,
    request_timeout,
    require_reconnectable_token,
    run_create_args,
    serve_command,
    startup_failure_message,
    template_name,
)
from rayito._agent._project_service import AGENT_SERVE_USER
from rayito._images import DEFAULT_BUILD_TIMEOUT_SECONDS
from rayito._limits import AGENT_SERVICE_READY_MARKER_PATH, AGENT_SERVICE_READY_TIMEOUT_SECONDS
from rayito.exceptions import SandboxException, TimeoutException

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Mapping

    from rayito._agent._project_domain import AgentProject
    from rayito._templates._models import BuildInfo

logger = logging.getLogger("rayito.agent")


class AsyncProjectService:
    """El espejo asíncrono de `ProjectService`."""

    def __init__(
        self,
        project: AgentProject,
        *,
        sandbox_class: Any = None,
        transport_factory: Any = default_async_transport,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._project = project
        self._sandbox_class = sandbox_class
        self._transport_factory = transport_factory
        self._sleep = sleep
        self._clock = clock

    async def build(
        self,
        *,
        bucket: str,
        force: bool = False,
        timeout: float = DEFAULT_BUILD_TIMEOUT_SECONDS,
        on_build_logs: Callable[[str], None] | None = None,
        region: str | None = None,
        session: Any = None,
        base_version: str | None = None,
    ) -> BuildInfo:
        """Construye la imagen del proyecto con `AsyncAgentTemplate.build()`;
        `base_version` fija la versión de la base (`None`: la más reciente)."""
        template: Any = project_template(
            self._project, asynchronous=True, base_version=base_version
        )
        return await template.build(  # type: ignore[no-any-return]
            bucket=bucket,
            force=force,
            timeout=timeout,
            on_build_logs=on_build_logs,
            region=region,
            session=session,
        )

    async def run(
        self,
        prompt: str,
        *,
        session_id: str | None = None,
        model: str | None = None,
        reasoning: bool = False,
        on_event: Callable[[AgentEvent], None] | None = None,
        region: str | None = None,
        session: Any = None,
    ) -> AgentResult:
        """Un sandbox desde la imagen del proyecto, una ejecución con
        `sbx.agent.run()` y `kill()` siempre, también ante un error."""
        project = self._project
        sandbox = await self._create(run_create_args(project), region, session)
        try:
            stream = await sandbox.agent.stream(
                prompt,
                spec=project.spec,
                runtime=project.runtime,
                session_id=session_id,
                model=model,
                limits=project.limits,
                reasoning=reasoning,
            )
            try:
                if on_event is not None:
                    async for event in stream:
                        on_event(event)
                return await stream.result()  # type: ignore[no-any-return]
            finally:
                await stream.aclose()
        finally:
            await self._kill(sandbox)

    async def deploy(
        self,
        *,
        region: str | None = None,
        session: Any = None,
        access_token: str | None = None,
        keep_on_failure: bool = False,
        metadata: Mapping[str, str] | None = None,
        index: Any = None,
    ) -> AsyncAgentDeployment:
        """Sirve el agente desde un MicroVM y devuelve su
        `AsyncAgentDeployment`. Si cualquier paso falla, mata el sandbox
        antes de propagar (salvo con `keep_on_failure=True`, que lo deja
        vivo y lo anota en el error). `metadata` e `index` son para una flota
        (ver `deploy_create_args`)."""
        project = self._project
        if keep_on_failure:
            require_reconnectable_token(access_token)
        sandbox = await self._create(
            deploy_create_args(
                project,
                raw=is_raw_project(project),
                access_token=access_token,
                metadata=metadata,
                index=index,
            ),
            region,
            session,
        )
        try:
            return await self.serve(sandbox)
        except BaseException as exc:
            if keep_on_failure and isinstance(exc, Exception):
                exc.add_note(kept_sandbox_note(sandbox))
            else:
                await self._kill(sandbox)
            raise

    async def serve(self, sandbox: Any) -> AsyncAgentDeployment:
        """Arranca el host del agente en un sandbox ya creado (o tomado de un
        pool con las pasarelas del proyecto) y espera su marcador de listo.
        No mata el sandbox si falla: lo decide quien lo creó."""
        project = self._project
        host = host_config_for(project, sandbox.gateways)
        await sandbox.files.write_files(host_entries(project, host))
        await sandbox.commands.run(clear_markers_command(), user=AGENT_SERVE_USER)
        handle = await sandbox.commands.run(
            serve_command(host, project.serve.port),
            background=True,
            user=AGENT_SERVE_USER,
            envs=dict(host.envs),
            timeout=None,
            tag=AGENT_SERVE_TAG,
            kill_tree=True,
        )
        handle.disconnect()
        await self._wait_ready(sandbox)
        return AsyncAgentDeployment(
            sandbox,
            port=project.serve.port,
            host_access=await sandbox.get_host(project.serve.port),
            project=project.name,
            raw=is_raw_app(host),
            request_timeout=request_timeout(project),
            transport_factory=self._transport_factory,
        )

    async def _create(self, arguments: dict[str, Any], region: str | None, session: Any) -> Any:
        sandbox_class = self._sandbox_class
        if sandbox_class is None:
            from rayito.sandbox_async import AsyncSandbox

            sandbox_class = AsyncSandbox
        extra = {key: value for key, value in (("region", region), ("session", session)) if value}
        return await sandbox_class.create(template_name(self._project), **arguments, **extra)

    async def _wait_ready(self, sandbox: Any) -> None:
        """Espera al marcador de listo del host: `TimeoutException` al
        agotar `AGENT_SERVICE_READY_TIMEOUT_SECONDS` y `SandboxException` si
        el host dejó un marcador de arranque fallido."""
        deadline = self._clock() + AGENT_SERVICE_READY_TIMEOUT_SECONDS
        while True:
            if await sandbox.files.exists(AGENT_SERVICE_READY_MARKER_PATH):
                return
            if await sandbox.files.exists(failed_marker_path()):
                marker = await sandbox.files.read(failed_marker_path())
                raise SandboxException(startup_failure_message(marker))
            if self._clock() >= deadline:
                raise TimeoutException("el host del agente no estuvo listo a tiempo")
            await self._sleep(AGENT_SERVICE_READY_POLL_SECONDS)

    @staticmethod
    async def _kill(sandbox: Any) -> None:
        """Mata el sandbox sin tapar la excepción original; un fallo del
        propio `kill()` se registra (sin datos del sandbox)."""
        try:
            await sandbox.kill()
        except Exception:
            logger.warning("no se pudo terminar el sandbox del agente", exc_info=True)


__all__ = ["AsyncProjectService"]
