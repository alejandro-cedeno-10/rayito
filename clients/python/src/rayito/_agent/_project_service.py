"""`AgentProject.build()`, `run()` y `deploy()` síncronos (ADR-028, D6):
la capa de aplicación sobre `AgentTemplate`, `Sandbox` y `AgentDeployment`.
Nada aquí se ejecuta al construir o cargar un proyecto; cada operación es
la única que crea recursos y `deploy()` mata su sandbox ante cualquier
fallo (salvo `keep_on_failure`).
"""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING, Any, Final

from rayito._agent._deployment import AgentDeployment, default_transport
from rayito._agent._events import AgentEvent, AgentResult
from rayito._agent._project_base import (
    AGENT_SERVE_TAG,
    AGENT_SERVICE_READY_POLL_SECONDS,
    clear_markers_command,
    deploy_create_args,
    failed_marker_path,
    host_config_for,
    host_entries,
    image_version_of,
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
    version_args,
)
from rayito._agent._runtime import HostConfig
from rayito._agent._secret_env_domain import (
    SecretReader,
    injected_names_environment,
    resolve_secret_env,
    spec_document,
)
from rayito._agent._secret_env_project import check_runtime_collisions
from rayito._agent._secret_env_reader import secret_reader_for
from rayito._images import DEFAULT_BUILD_TIMEOUT_SECONDS
from rayito._limits import AGENT_SERVICE_READY_MARKER_PATH, AGENT_SERVICE_READY_TIMEOUT_SECONDS
from rayito.exceptions import SandboxException, TimeoutException

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from rayito._agent._project_domain import AgentProject
    from rayito._templates._models import BuildInfo

logger = logging.getLogger("rayito.agent")

#: Usuario con el que corre el host del agente (uid 1000): nunca root.
AGENT_SERVE_USER: Final = "user"


class ProjectService:
    """Las tres operaciones que cuestan de un `AgentProject`. El proyecto,
    la clase de sandbox y el transporte llegan por constructor, así que los
    tests usan dobles y nunca AWS."""

    def __init__(
        self,
        project: AgentProject,
        *,
        sandbox_class: Any = None,
        transport_factory: Any = default_transport,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        image_version: str | None = None,
        secret_reader: SecretReader | None = None,
    ) -> None:
        self._project = project
        self._secret_reader = secret_reader
        self._sandbox_class = sandbox_class
        self._transport_factory = transport_factory
        self._sleep = sleep
        self._clock = clock
        self._image_version = image_version

    @property
    def image_version(self) -> str | None:
        """La versión de la imagen con la que se crean los sandboxes: la que
        fijó el constructor o el último `build()` (creada o reutilizada), o
        `None` para la más reciente activa."""
        return self._image_version

    def build(
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
        """Construye la imagen del proyecto con `AgentTemplate.build()`;
        `base_version` fija la versión de la base (`None`: la más reciente).
        La versión que el build crea o reutiliza queda fijada para los
        sandboxes que este servicio cree después, aunque la imagen tenga otra
        más reciente."""
        info = project_template(self._project, base_version=base_version).build(
            bucket=bucket,
            force=force,
            timeout=timeout,
            on_build_logs=on_build_logs,
            region=region,
            session=session,
        )
        self._image_version = image_version_of(info)
        return info

    def run(
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
        sandbox = self._create(run_create_args(project), region, session)
        try:
            with sandbox.agent.stream(
                prompt,
                spec=project.spec,
                runtime=project.runtime,
                session_id=session_id,
                model=model,
                limits=project.limits,
                reasoning=reasoning,
            ) as stream:
                if on_event is not None:
                    for event in stream:
                        on_event(event)
                return stream.result()  # type: ignore[no-any-return]
        finally:
            self._kill(sandbox)

    def deploy(
        self,
        *,
        region: str | None = None,
        session: Any = None,
        access_token: str | None = None,
        keep_on_failure: bool = False,
        metadata: Mapping[str, str] | None = None,
        index: Any = None,
        lifetime: Mapping[str, Any] | None = None,
    ) -> AgentDeployment:
        """Sirve el agente desde un MicroVM y devuelve su
        `AgentDeployment`. Si cualquier paso falla, mata el sandbox antes de
        propagar. `metadata`, `index` y `lifetime` son para una flota (ver
        `deploy_create_args`)."""
        project = self._project
        if keep_on_failure:
            require_reconnectable_token(access_token)
        sandbox = self._create(
            deploy_create_args(
                project,
                raw=is_raw_project(project),
                access_token=access_token,
                metadata=metadata,
                index=index,
                lifetime=lifetime,
            ),
            region,
            session,
        )
        try:
            return self.serve(sandbox)
        except BaseException as exc:
            if keep_on_failure and isinstance(exc, Exception):
                exc.add_note(kept_sandbox_note(sandbox))
            else:
                self._kill(sandbox)
            raise

    def serve(self, sandbox: Any) -> AgentDeployment:
        """Arranca el host del agente en un sandbox ya creado (o tomado de un
        pool con las pasarelas del proyecto) y espera su marcador de listo.
        No mata el sandbox si falla: lo decide quien lo creó."""
        project = self._project
        host = host_config_for(project, sandbox.gateways)
        secret_env = self._secret_env(sandbox, host)
        sandbox.files.write_files(host_entries(project, host))
        sandbox.commands.run(clear_markers_command(), user=AGENT_SERVE_USER)
        handle = sandbox.commands.run(
            serve_command(host, project.serve.port),
            background=True,
            user=AGENT_SERVE_USER,
            envs={**host.envs, **secret_env, **injected_names_environment(secret_env)},
            timeout=None,
            tag=AGENT_SERVE_TAG,
            kill_tree=True,
        )
        handle.disconnect()
        self._wait_ready(sandbox)
        return AgentDeployment(
            sandbox,
            port=project.serve.port,
            project=project.name,
            raw=is_raw_app(host),
            request_timeout=request_timeout(project),
            transport_factory=self._transport_factory,
        )

    def _secret_env(self, sandbox: Any, host: HostConfig) -> dict[str, str]:
        """Las variables de `secret_env` y `secret_env_from`, leídas con las
        credenciales de quien despliega (`secret_reader`, o las de boto3 en
        la región del sandbox); nada, y ninguna llamada, sin ellas. Viajan
        sólo en el entorno del proceso del host, nunca en un fichero."""
        project = self._project
        spec = spec_document(project.secret_env, project.secret_env_from)
        if spec is None:
            return {}
        check_runtime_collisions(project, host.envs)
        reader = self._secret_reader or secret_reader_for(sandbox.region)
        return resolve_secret_env(spec, reader, tuple(host.envs))

    def _create(self, arguments: dict[str, Any], region: str | None, session: Any) -> Any:
        sandbox_class = self._sandbox_class
        if sandbox_class is None:
            from rayito.sandbox_sync import Sandbox

            sandbox_class = Sandbox
        extra = {key: value for key, value in (("region", region), ("session", session)) if value}
        return sandbox_class.create(
            template_name(self._project),
            **arguments,
            **version_args(self._image_version),
            **extra,
        )

    def _wait_ready(self, sandbox: Any) -> None:
        """Espera al marcador de listo del host: `TimeoutException` al
        agotar `AGENT_SERVICE_READY_TIMEOUT_SECONDS` y `SandboxException` si
        el host dejó un marcador de arranque fallido."""
        deadline = self._clock() + AGENT_SERVICE_READY_TIMEOUT_SECONDS
        while True:
            if sandbox.files.exists(AGENT_SERVICE_READY_MARKER_PATH):
                return
            if sandbox.files.exists(failed_marker_path()):
                marker = sandbox.files.read(failed_marker_path())
                raise SandboxException(startup_failure_message(marker))
            if self._clock() >= deadline:
                raise TimeoutException("el host del agente no estuvo listo a tiempo")
            self._sleep(AGENT_SERVICE_READY_POLL_SECONDS)

    @staticmethod
    def _kill(sandbox: Any) -> None:
        """Mata el sandbox sin tapar la excepción original; un fallo del
        propio `kill()` se registra (sin datos del sandbox)."""
        try:
            sandbox.kill()
        except Exception:
            logger.warning("no se pudo terminar el sandbox del agente", exc_info=True)


__all__ = ["ProjectService"]
