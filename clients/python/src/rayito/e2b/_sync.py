"""`rayito.e2b.Sandbox`: la superficie de `e2b_code_interpreter.Sandbox` /
`e2b.Sandbox` (SDK 2.51) sobre un `rayito.Sandbox` nativo por composición.

`Sandbox.create(...)` lleva la firma posicional de E2B 2.x y los
`ApiParams` como keywords; el constructor deprecado conserva la superficie
1.x (con `sandbox_id` se conecta). `commands`, `files` y `pty` son wrappers
finos con las firmas posicionales de E2B que devuelven los handles
nativos; `git` es el `Git` nativo. Todo lo que Lambda MicroVMs no puede
hacer lanza `UnimplementedError` antes de tocar AWS o el agente. El nativo
queda accesible en `sbx.native`.
"""

from __future__ import annotations

import builtins
import logging
import os
import warnings
from collections.abc import Iterator, Mapping, Sequence
from datetime import datetime
from types import MappingProxyType, TracebackType
from typing import IO, Any, ClassVar, Literal, Self, Unpack, overload

from rayito import Sandbox as NativeSandbox
from rayito._code_base import ContextLike, ErrorCallback, ResultCallback, StdoutCallback
from rayito._filesystem_base import EventCallback, ExitCallback
from rayito._limits import DEFAULT_PORT
from rayito._models import (
    PROXY_AUTH_HEADER,
    CodeContext,
    CommandResult,
    DownloadLink,
    EntryInfo,
    Execution,
    HostAccess,
    NetworkState,
    ProcessInfo,
    UploadTicket,
    WriteEntry,
)
from rayito._process_base import OutputCallback
from rayito._pty_base import PtyDataCallback
from rayito._sandbox_base import (
    DEFAULT_READY_TIMEOUT_SECONDS,
    DEFAULT_RECONNECT_TIMEOUT_SECONDS,
    LoggingOption,
    PortLike,
    class_method_variant,
)
from rayito.e2b._compat import (
    NativeCall,
    class_metrics_unimplemented,
    create_only_kwargs_given,
    history_falls_back_to_snapshot,
    info_from_native,
    lifecycle_unimplemented,
    list_item_to_info,
    list_mapping,
    map_create_kwargs,
    map_network_update,
    metrics_from_native,
    native_background,
    native_call_kwargs,
    native_stdin,
    needs_metrics_snapshot,
    normalized_language_or_unimplemented,
    pty_size_to_native,
    reject_callable_in_user_slot,
    resolve_metrics_token,
    unimplemented_language,
    validate_keep_memory,
    validate_on_resume,
)
from rayito.e2b._connection import (
    ApiParams,
    ConnectionConfig,
    ConnectionParams,
    snapshot_config,
    split_api_params,
)
from rayito.e2b._models import (
    PtySize,
    SandboxInfo,
    SandboxMetrics,
    SandboxPaginator,
    SandboxQuery,
    SandboxState,
)
from rayito.e2b._unimplemented import UnimplementedMember
from rayito.e2b.exceptions import RayitoCompatWarning
from rayito.exceptions import (
    InvalidArgumentException,
    LifecycleUnsupportedException,
    UnimplementedError,
)
from rayito.sandbox_sync.commands import CommandHandle
from rayito.sandbox_sync.commands import Commands as NativeCommands
from rayito.sandbox_sync.filesystem import Filesystem as NativeFilesystem
from rayito.sandbox_sync.filesystem import WatchHandle
from rayito.sandbox_sync.git import Git
from rayito.sandbox_sync.pty import Pty as NativePty
from rayito.sandbox_sync.pty import PtyHandle

E2B_WATCH_TIMEOUT_SECONDS = 60
E2B_PTY_TIMEOUT_SECONDS = 60
E2B_COMMAND_TIMEOUT_SECONDS = 60
WARN_STACKLEVEL = 3
EMPTY_PARAMS: Mapping[str, Any] = MappingProxyType({})

MISSING_UPLOAD_PATH_MESSAGE = "indica la ruta de destino: una URL de S3 no lleva nombre de fichero"
IGNORED_ON_CONNECT_REASON = "Sandbox(sandbox_id=...) se conecta y no aplica: "


def emit_warnings(messages: Sequence[str]) -> None:
    for message in messages:
        warnings.warn(message, RayitoCompatWarning, stacklevel=WARN_STACKLEVEL)


def request_timeout_of(api_params: Mapping[str, Any], *, call: str) -> float | None:
    """Los `ApiParams` de una llamada de instancia: avisa de los ignorados y
    devuelve el `request_timeout`."""
    settings, messages = split_api_params(api_params, call=call)
    emit_warnings(messages)
    return settings.request_timeout


class Sandbox:
    """Drop-in de `e2b_code_interpreter.Sandbox` / `e2b.Sandbox` (2.51)."""

    _bound_params: ClassVar[Mapping[str, Any]] = EMPTY_PARAMS

    def __init__(
        self,
        template: str | None = None,
        timeout: int | None = None,
        metadata: Mapping[str, str] | None = None,
        envs: Mapping[str, str] | None = None,
        api_key: str | None = None,
        domain: str | None = None,
        debug: bool = False,
        sandbox_id: str | None = None,
        request_timeout: float | None = None,
        proxy: str | None = None,
        secure: bool = True,
        allow_internet_access: bool = True,
        *,
        lifecycle: Mapping[str, Any] | None = None,
        network: Mapping[str, Any] | None = None,
        max_lifetime: int | None = None,
        logger: logging.Logger | None = None,
        retries: int | None = None,
        headers: Mapping[str, str] | None = None,
        region: str | None = None,
        session: Any | None = None,
        template_version: str | None = None,
        execution_role_arn: str | None = None,
        allowed_ports: Sequence[PortLike] | None = None,
        ingress: Sequence[str] | None = None,
        logging: LoggingOption = "disabled",
        access_token: str | None = None,
        ready_timeout: float = DEFAULT_READY_TIMEOUT_SECONDS,
        reconnect_timeout: float = DEFAULT_RECONNECT_TIMEOUT_SECONDS,
        keep_on_failure: bool = False,
        control_plane: Any | None = None,
        transport: Any | None = None,
        _native: NativeSandbox | None = None,
        _connection: ConnectionConfig | None = None,
    ) -> None:
        """Constructor deprecado de E2B: crea el sandbox con la misma tabla
        que `create()` o, con `sandbox_id`, se conecta (E2B v1). Los kwargs
        de creación dados junto a `sandbox_id` avisan y no se aplican."""
        if _native is not None:
            self._bind(_native, _connection)
            return
        api_params: dict[str, Any] = {
            "api_key": api_key,
            "domain": domain,
            "debug": debug,
            "request_timeout": request_timeout,
            "proxy": proxy,
            "retries": retries,
            "headers": headers,
        }
        if sandbox_id is not None:
            self._warn_create_only_kwargs(
                template=template,
                timeout=timeout,
                metadata=metadata,
                envs=envs,
                lifecycle=lifecycle,
                network=network,
                max_lifetime=max_lifetime,
                template_version=template_version,
                execution_role_arn=execution_role_arn,
                allowed_ports=allowed_ports,
                ingress=ingress,
                allow_internet_access=None if allow_internet_access else False,
                secure=None if secure else False,
            )
            native, config = type(self)._connect_native(
                sandbox_id,
                None,
                logger=logger,
                access_token=access_token,
                region=region,
                session=session,
                ready_timeout=ready_timeout,
                reconnect_timeout=reconnect_timeout,
                control_plane=control_plane,
                transport=transport,
                api_params=api_params,
            )
            self._bind(native, config)
            return
        native, config = type(self)._launch(
            {
                "template": template,
                "timeout": timeout,
                "metadata": metadata,
                "envs": envs,
                "secure": secure,
                "allow_internet_access": allow_internet_access,
                "network": network,
                "lifecycle": lifecycle,
                "logger": logger,
                "max_lifetime": max_lifetime,
                "region": region,
                "session": session,
                "template_version": template_version,
                "execution_role_arn": execution_role_arn,
                "allowed_ports": allowed_ports,
                "ingress": ingress,
                "logging": logging,
                "access_token": access_token,
                "ready_timeout": ready_timeout,
                "reconnect_timeout": reconnect_timeout,
                "keep_on_failure": keep_on_failure,
                "control_plane": control_plane,
                "transport": transport,
            },
            api_params,
        )
        self._bind(native, config)

    def _bind(self, native: NativeSandbox, connection: ConnectionConfig | None) -> None:
        self._native = native
        self._connection = connection
        self.commands = Commands(native.commands)
        self.files = Filesystem(native.files)
        self.pty = Pty(native.pty)

    @staticmethod
    def _warn_create_only_kwargs(**kwargs: Any) -> None:
        ignored = create_only_kwargs_given(**kwargs)
        if ignored:
            emit_warnings((IGNORED_ON_CONNECT_REASON + ", ".join(ignored),))

    @classmethod
    def _native_call(
        cls,
        native_kwargs: Mapping[str, Any],
        api_params: Mapping[str, Any],
        *,
        call: str,
        with_transport: bool = True,
        with_request_timeout: bool = True,
    ) -> NativeCall:
        resolved = native_call_kwargs(
            cls._bound_params,
            native_kwargs,
            api_params,
            call=call,
            with_transport=with_transport,
            with_request_timeout=with_request_timeout,
        )
        emit_warnings(resolved.warnings)
        return resolved

    @classmethod
    def _launch(
        cls, create_kwargs: Mapping[str, Any], api_params: Mapping[str, Any]
    ) -> tuple[NativeSandbox, ConnectionConfig]:
        mapping = map_create_kwargs(**create_kwargs)
        resolved = cls._native_call(mapping.native_kwargs, api_params, call="create")
        emit_warnings(mapping.warnings)
        try:
            native = NativeSandbox.create(**resolved.kwargs)
        except LifecycleUnsupportedException as exc:
            raise lifecycle_unimplemented() from exc
        config = snapshot_config(
            resolved.settings,
            region=native.region,
            logger=resolved.kwargs.get("logger"),
            integration=resolved.integration,
        )
        return native, config

    @classmethod
    def _connect_native(
        cls,
        sandbox_id: str,
        timeout: int | None,
        *,
        logger: logging.Logger | None,
        access_token: str | None,
        region: str | None,
        session: Any | None,
        ready_timeout: float,
        reconnect_timeout: float,
        control_plane: Any | None,
        transport: Any | None,
        api_params: Mapping[str, Any],
    ) -> tuple[NativeSandbox, ConnectionConfig]:
        resolved = cls._native_call(
            {
                "timeout": timeout,
                "logger": logger,
                "access_token": access_token,
                "region": region,
                "session": session,
                "ready_timeout": ready_timeout,
                "reconnect_timeout": reconnect_timeout,
                "control_plane": control_plane,
                "transport": transport,
            },
            api_params,
            call="connect",
        )
        try:
            native = NativeSandbox.connect(sandbox_id, **resolved.kwargs)
        except LifecycleUnsupportedException as exc:
            raise lifecycle_unimplemented() from exc
        config = snapshot_config(
            resolved.settings,
            region=native.region,
            logger=logger,
            integration=resolved.integration,
        )
        return native, config

    # ------------------------------------------------------------ classmethods

    @classmethod
    def create(
        cls,
        template: str | None = None,
        timeout: int | None = None,
        metadata: Mapping[str, str] | None = None,
        envs: Mapping[str, str] | None = None,
        secure: bool | None = None,
        allow_internet_access: bool | None = None,
        mcp: Any | None = None,
        network: Mapping[str, Any] | None = None,
        iam: Any | None = None,
        lifecycle: Mapping[str, Any] | None = None,
        volume_mounts: Any | None = None,
        logger: logging.Logger | None = None,
        *,
        max_lifetime: int | None = None,
        region: str | None = None,
        session: Any | None = None,
        template_version: str | None = None,
        execution_role_arn: str | None = None,
        allowed_ports: Sequence[PortLike] | None = None,
        ingress: Sequence[str] | None = None,
        logging: LoggingOption = "disabled",
        access_token: str | None = None,
        ready_timeout: float = DEFAULT_READY_TIMEOUT_SECONDS,
        reconnect_timeout: float = DEFAULT_RECONNECT_TIMEOUT_SECONDS,
        keep_on_failure: bool = False,
        control_plane: Any | None = None,
        transport: Any | None = None,
        **api_params: Unpack[ApiParams],
    ) -> Self:
        """`Sandbox.create(...)` de E2B 2.x (tabla D5): `timeout` (300 s por
        defecto) es el plazo lógico que impone `rayd` y `max_lifetime` la vida
        de plataforma; `lifecycle` decide si al vencer se termina o se pausa;
        `allow_internet_access=False` y `network` son la política de egress en
        el guest (`rayito-base-caps`); `mcp`, `iam` y `volume_mounts` son
        `UnimplementedError`. `headers`, `proxy` y `retries` llegan al canal
        y al plano; `api_key`, `domain`, `debug`, `api_url`, `sandbox_url`,
        `validate_api_key`, `api_headers` y `secure=False` avisan con un
        `RayitoCompatWarning` cada uno. Sobre una imagen anterior a M9 el VM
        se termina y es `UnimplementedError`."""
        return cls._create(
            template,
            timeout,
            metadata,
            envs,
            secure,
            allow_internet_access,
            mcp,
            network,
            iam,
            lifecycle,
            volume_mounts,
            logger,
            max_lifetime=max_lifetime,
            auto_pause=None,
            region=region,
            session=session,
            template_version=template_version,
            execution_role_arn=execution_role_arn,
            allowed_ports=allowed_ports,
            ingress=ingress,
            logging=logging,
            access_token=access_token,
            ready_timeout=ready_timeout,
            reconnect_timeout=reconnect_timeout,
            keep_on_failure=keep_on_failure,
            control_plane=control_plane,
            transport=transport,
            api_params=dict(api_params),
        )

    @classmethod
    def _create(
        cls,
        template: str | None,
        timeout: int | None,
        metadata: Mapping[str, str] | None,
        envs: Mapping[str, str] | None,
        secure: bool | None,
        allow_internet_access: bool | None,
        mcp: Any | None,
        network: Mapping[str, Any] | None,
        iam: Any | None,
        lifecycle: Mapping[str, Any] | None,
        volume_mounts: Any | None,
        logger: logging.Logger | None,
        *,
        max_lifetime: int | None,
        auto_pause: bool | None,
        api_params: Mapping[str, Any],
        **native_kwargs: Any,
    ) -> Self:
        native, config = cls._launch(
            {
                "template": template,
                "timeout": timeout,
                "metadata": metadata,
                "envs": envs,
                "secure": secure,
                "allow_internet_access": allow_internet_access,
                "mcp": mcp,
                "network": network,
                "iam": iam,
                "lifecycle": lifecycle,
                "volume_mounts": volume_mounts,
                "logger": logger,
                "max_lifetime": max_lifetime,
                "auto_pause": auto_pause,
                **native_kwargs,
            },
            api_params,
        )
        return cls(_native=native, _connection=config)

    @classmethod
    def beta_create(
        cls,
        template: str | None = None,
        timeout: int | None = None,
        auto_pause: bool = False,
        allow_internet_access: bool | None = None,
        metadata: Mapping[str, str] | None = None,
        envs: Mapping[str, str] | None = None,
        secure: bool | None = None,
        mcp: Any | None = None,
        network: Mapping[str, Any] | None = None,
        *,
        lifecycle: Mapping[str, Any] | None = None,
        logger: logging.Logger | None = None,
        max_lifetime: int | None = None,
        **kwargs: Any,
    ) -> Self:
        """`Sandbox.beta_create`: como `create`, con `auto_pause=True` como
        `lifecycle={"on_timeout": "pause"}` (sin auto-resume); `network` se
        mapea igual que en `create` y `mcp` es `UnimplementedError`."""
        api_params = {
            key: kwargs.pop(key) for key in list(kwargs) if key in ApiParams.__optional_keys__
        }
        return cls._create(
            template,
            timeout,
            metadata,
            envs,
            secure,
            allow_internet_access,
            mcp,
            network,
            None,
            lifecycle,
            None,
            logger,
            max_lifetime=max_lifetime,
            auto_pause=True if auto_pause else None,
            api_params=api_params,
            **kwargs,
        )

    @class_method_variant("_class_connect")
    def connect(
        self,
        timeout: int | None = None,
        *,
        on_resume: Literal["restore", "reboot"] = "restore",
        **api_params: Unpack[ApiParams],
    ) -> Sandbox:
        """`sbx.connect()`: sobre el nativo ya enlazado hace `get-microvm`,
        reanuda si estaba pausado, espera a `Health` y, con `timeout`,
        extiende el plazo a al menos ahora + `timeout`. Devuelve `self`.
        `on_resume='reboot'` es `UnimplementedError`."""
        validate_on_resume(on_resume)
        request_timeout = request_timeout_of(api_params, call="connect")
        try:
            self._native.connect(timeout=timeout, request_timeout=request_timeout)
        except LifecycleUnsupportedException as exc:
            raise lifecycle_unimplemented() from exc
        return self

    @classmethod
    def _class_connect(
        cls,
        sandbox_id: str,
        timeout: int | None = None,
        *,
        on_resume: Literal["restore", "reboot"] = "restore",
        logger: logging.Logger | None = None,
        access_token: str | None = None,
        region: str | None = None,
        session: Any | None = None,
        ready_timeout: float = DEFAULT_READY_TIMEOUT_SECONDS,
        reconnect_timeout: float = DEFAULT_RECONNECT_TIMEOUT_SECONDS,
        control_plane: Any | None = None,
        transport: Any | None = None,
        **api_params: Unpack[ApiParams],
    ) -> Self:
        """`Sandbox.connect(sandbox_id, timeout)`: se engancha (y reanuda uno
        pausado); con `timeout` el plazo pasa a ser al menos ahora + `timeout`.
        Necesita el `access_token` del sandbox o `RAYITO_ACCESS_TOKEN`."""
        validate_on_resume(on_resume)
        native, config = cls._connect_native(
            sandbox_id,
            timeout,
            logger=logger,
            access_token=access_token,
            region=region,
            session=session,
            ready_timeout=ready_timeout,
            reconnect_timeout=reconnect_timeout,
            control_plane=control_plane,
            transport=transport,
            api_params=dict(api_params),
        )
        return cls(_native=native, _connection=config)

    @classmethod
    def list(
        cls,
        query: SandboxQuery | None = None,
        limit: int | None = None,
        next_token: str | None = None,
        *,
        state: Sequence[SandboxState] | None = None,
        order: Literal["asc", "desc"] | None = None,
        template: str | None = None,
        template_version: str | None = None,
        region: str | None = None,
        session: Any | None = None,
        control_plane: Any | None = None,
        transport: Any | None = None,
        **api_params: Unpack[ApiParams],
    ) -> SandboxPaginator:
        """`SandboxPaginator` sobre `rayito.Sandbox.paginate`: `limit` es el
        tamaño de página y `next_token` reanuda un listado anterior. Con
        `query.metadata` el filtro es O(n) sobre los sandboxes `RUNNING` (un
        `Health` por sandbox, dentro de `next_items()`)."""
        mapping = list_mapping(query, state, template)
        resolved = cls._native_call(
            {
                "template": mapping.template,
                "template_version": template_version,
                "states": mapping.states,
                "metadata": mapping.metadata,
                "started_after": mapping.started_after,
                "order": order,
                "limit": limit,
                "next_token": next_token,
                "region": region,
                "session": session,
                "control_plane": control_plane,
                "transport": transport,
            },
            api_params,
            call="list",
        )
        return SandboxPaginator(NativeSandbox.paginate(**resolved.kwargs), mapper=list_item_to_info)

    # -------------------------------------------------------------- properties

    @property
    def native(self) -> NativeSandbox:
        """El `rayito.Sandbox` de debajo (`get_health`, `access_token`, `pty.create(shell=)`...)."""
        return self._native

    @property
    def sandbox_id(self) -> str:
        return self._native.sandbox_id

    @property
    def sandbox_domain(self) -> str:
        """El hostname del endpoint del MicroVM: el único dominio que tiene."""
        return self._native.endpoint

    @property
    def envd_api_url(self) -> str:
        """`https://<endpoint>`; toda petición necesita además `get_host(8080).headers`."""
        return self._native.endpoint_url

    @property
    def envd_direct_url(self) -> str:
        """Igual que `envd_api_url`: el endpoint es el único acceso directo."""
        return self._native.endpoint_url

    @property
    def traffic_access_token(self) -> str:
        """El JWE del proxy vigente para el puerto 8080 de `rayd`. Es una
        credencial al portador del endpoint (rota a los 45 min, vive 60 como
        mucho): nunca la loguees."""
        return self._native.get_host(DEFAULT_PORT).headers[PROXY_AUTH_HEADER]

    @property
    def connection_config(self) -> ConnectionConfig:
        """La configuración efectiva: región, `request_timeout`, `retries`,
        `headers`, `proxy` y `logger` dados al crear o conectar, y la
        integración vigente entonces."""
        if self._connection is None:
            self._connection = snapshot_config(
                split_api_params({}, call="connection_config")[0],
                region=self._native.region,
                logger=None,
                integration=ConnectionConfig.current_integration(),
            )
        return self._connection

    @property
    def git(self) -> Git:
        """El `rayito.Git` nativo (comandos `git` con `GIT_TERMINAL_PROMPT=0`)."""
        return self._native.git

    # --------------------------------------------------------------- lifecycle

    def is_running(self, request_timeout: float | None = None) -> bool:
        """`Health` con `agent_ready`, con la sonda acotada por `request_timeout`."""
        return self._native.is_running(request_timeout=request_timeout)

    @class_method_variant("_class_kill")
    def kill(self, **api_params: Unpack[ApiParams]) -> bool:
        request_timeout_of(api_params, call="kill")
        return bool(self._native.kill())

    @classmethod
    def _class_kill(
        cls,
        sandbox_id: str,
        *,
        region: str | None = None,
        session: Any | None = None,
        control_plane: Any | None = None,
        **api_params: Unpack[ApiParams],
    ) -> bool:
        resolved = cls._native_call(
            {"region": region, "session": session, "control_plane": control_plane},
            api_params,
            call="kill",
            with_transport=False,
            with_request_timeout=False,
        )
        return bool(NativeSandbox.kill(sandbox_id, **resolved.kwargs))

    @class_method_variant("_class_get_info")
    def get_info(self, request_timeout: float | None = None) -> SandboxInfo:
        """`SandboxInfo` 2.x: `get-microvm` fresco, metadatos y hechos del
        guest del último `Health` y, sobre un sandbox `RUNNING`, la política
        de egress (`GetNetwork`) para `network` y `allow_internet_access`."""
        info = self._native.get_info()
        network, network_read = self._read_network(info.state, request_timeout)
        return info_from_native(info, network=network, network_read=network_read)

    def _read_network(
        self, state: str, request_timeout: float | None
    ) -> tuple[NetworkState | None, bool]:
        if state != "RUNNING":
            return None, False
        try:
            return self._native.get_network(request_timeout=request_timeout), True
        except UnimplementedError:
            return None, True

    @classmethod
    def _class_get_info(
        cls,
        sandbox_id: str,
        request_timeout: float | None = None,
        *,
        region: str | None = None,
        session: Any | None = None,
        control_plane: Any | None = None,
        transport: Any | None = None,
        **api_params: Unpack[ConnectionParams],
    ) -> SandboxInfo:
        """`Sandbox.get_info(sandbox_id)`: lee los metadatos con un `Health`
        cuando el sandbox está `RUNNING`; un sandbox terminado es
        `NotFoundException` (el 404 de E2B)."""
        resolved = cls._native_call(
            {
                "region": region,
                "session": session,
                "control_plane": control_plane,
                "transport": transport,
            },
            {**api_params, "request_timeout": request_timeout},
            call="get_info",
        )
        return info_from_native(NativeSandbox.get_info(sandbox_id, **resolved.kwargs))

    @class_method_variant("_class_set_timeout")
    def set_timeout(self, timeout: int, request_timeout: float | None = None) -> None:
        """Fija el plazo lógico en ahora + `timeout` segundos (`SetTimeout`
        EXACT): puede alargarlo o acortarlo, con `max_lifetime` como tope."""
        self._native.set_timeout(timeout, request_timeout=request_timeout)

    @classmethod
    def _class_set_timeout(
        cls,
        sandbox_id: str,
        timeout: int,
        request_timeout: float | None = None,
        *,
        access_token: str | None = None,
        region: str | None = None,
        session: Any | None = None,
        control_plane: Any | None = None,
        transport: Any | None = None,
        **api_params: Unpack[ConnectionParams],
    ) -> None:
        """`Sandbox.set_timeout(sandbox_id, timeout)`: el mismo `SetTimeout`
        EXACT sin handle; necesita el access token o `RAYITO_ACCESS_TOKEN`."""
        resolved = cls._native_call(
            {
                "access_token": access_token,
                "region": region,
                "session": session,
                "control_plane": control_plane,
                "transport": transport,
            },
            {**api_params, "request_timeout": request_timeout},
            call="set_timeout",
        )
        NativeSandbox.set_timeout(sandbox_id, timeout, **resolved.kwargs)

    @class_method_variant("_class_get_metrics")
    def get_metrics(
        self,
        start: datetime | None = None,
        end: datetime | None = None,
        request_timeout: float | None = None,
    ) -> builtins.list[SandboxMetrics]:
        """La serie de métricas en bytes y en orden (`MetricsHistory`, una
        muestra cada 5 s). Sin rango y con el historial vacío o una imagen
        anterior a M9 devuelve la instantánea de `Metrics`; con rango en una
        imagen anterior a M9 es `UnimplementedError`."""
        ranged = start is not None or end is not None
        try:
            samples = self._native.get_metrics_history(
                start=start, end=end, request_timeout=request_timeout
            )
        except UnimplementedError as exc:
            if not history_falls_back_to_snapshot(exc, ranged=ranged):
                raise
            samples = []
        if needs_metrics_snapshot(samples, ranged=ranged):
            return [metrics_from_native(self._native.get_metrics(request_timeout=request_timeout))]
        return [metrics_from_native(sample) for sample in samples]

    @classmethod
    def _class_get_metrics(
        cls,
        sandbox_id: str,
        start: datetime | None = None,
        end: datetime | None = None,
        request_timeout: float | None = None,
        *,
        access_token: str | None = None,
        region: str | None = None,
        session: Any | None = None,
        control_plane: Any | None = None,
        transport: Any | None = None,
        **api_params: Unpack[ConnectionParams],
    ) -> builtins.list[SandboxMetrics]:
        """`Sandbox.get_metrics(sandbox_id)`: el historial sin handle. Sin
        `access_token` ni `RAYITO_ACCESS_TOKEN` es `UnimplementedError` antes
        de llamar a AWS."""
        token = resolve_metrics_token(access_token, os.environ)
        resolved = cls._native_call(
            {
                "region": region,
                "session": session,
                "control_plane": control_plane,
                "transport": transport,
            },
            {**api_params, "request_timeout": request_timeout},
            call="get_metrics",
        )
        try:
            samples = NativeSandbox.get_metrics_history(
                sandbox_id, access_token=token, start=start, end=end, **resolved.kwargs
            )
        except UnimplementedError as exc:
            mapped = class_metrics_unimplemented(exc)
            if mapped is None:
                raise
            raise mapped from exc
        return [metrics_from_native(sample) for sample in samples]

    @class_method_variant("_class_pause")
    def pause(self, keep_memory: bool | None = None, **api_params: Unpack[ApiParams]) -> bool:
        """`suspend-microvm` y espera a `SUSPENDED`: `True` si lo pausó,
        `False` si ya estaba `SUSPENDING|SUSPENDED`. `keep_memory=False` es
        `UnimplementedError` (la pausa siempre guarda memoria y disco)."""
        validate_keep_memory(keep_memory)
        request_timeout_of(api_params, call="pause")
        return self._native.pause(wait=True)

    @classmethod
    def _class_pause(
        cls,
        sandbox_id: str,
        keep_memory: bool | None = None,
        *,
        region: str | None = None,
        session: Any | None = None,
        control_plane: Any | None = None,
        **api_params: Unpack[ApiParams],
    ) -> bool:
        validate_keep_memory(keep_memory)
        resolved = cls._native_call(
            {"region": region, "session": session, "control_plane": control_plane},
            api_params,
            call="pause",
            with_transport=False,
            with_request_timeout=False,
        )
        return NativeSandbox.pause(sandbox_id, **resolved.kwargs)

    beta_pause = pause

    @class_method_variant("_class_update_network")
    def update_network(
        self,
        network: Mapping[str, Any] | None = None,
        request_timeout: float | None = None,
    ) -> None:
        """Sustituye la política de egress (`allow_out`, `deny_out`,
        `egress_proxy`, `allow_internet_access`); `rules` es
        `UnimplementedError`. Devuelve `None`, como E2B."""
        policy, allow_internet_access = map_network_update(network)
        self._native.update_network(
            policy, allow_internet_access=allow_internet_access, request_timeout=request_timeout
        )

    @classmethod
    def _class_update_network(
        cls,
        sandbox_id: str,
        network: Mapping[str, Any] | None = None,
        *,
        access_token: str | None = None,
        region: str | None = None,
        session: Any | None = None,
        control_plane: Any | None = None,
        transport: Any | None = None,
        **api_params: Unpack[ApiParams],
    ) -> None:
        """`Sandbox.update_network(sandbox_id, network)`: conecta, actualiza
        y cierra sin terminar el sandbox."""
        policy, allow_internet_access = map_network_update(network)
        resolved = cls._native_call(
            {
                "access_token": access_token,
                "region": region,
                "session": session,
                "control_plane": control_plane,
                "transport": transport,
            },
            api_params,
            call="update_network",
        )
        NativeSandbox.update_network(
            sandbox_id, policy, allow_internet_access=allow_internet_access, **resolved.kwargs
        )

    fork = UnimplementedMember("fork")
    create_snapshot = UnimplementedMember("create_snapshot")
    list_snapshots = UnimplementedMember("list_snapshots")
    delete_snapshot = UnimplementedMember("delete_snapshot")
    get_mcp_url = UnimplementedMember("get_mcp_url")
    get_mcp_token = UnimplementedMember("get_mcp_token")

    def upload_url(
        self,
        path: str | None = None,
        user: str | None = None,
        use_signature: bool = False,
        use_signature_expiration: int | None = None,
    ) -> UploadTicket:
        """El `UploadTicket` nativo: un `str` con la URL, así que `requests.put(url,
        data=f, headers=url.headers)` funciona y `wait()` espera la importación.
        `use_signature` se ignora porque una URL de S3 siempre va firmada;
        `use_signature_expiration` son los segundos de vida (`<= 0` es
        `InvalidArgumentException`); sin ruta no hay dónde importar el objeto."""
        if path is None:
            raise InvalidArgumentException(MISSING_UPLOAD_PATH_MESSAGE)
        return self._native.upload_url(
            path, user=user, use_signature_expiration=use_signature_expiration
        )

    def download_url(
        self,
        path: str,
        user: str | None = None,
        use_signature: bool = False,
        use_signature_expiration: int | None = None,
    ) -> DownloadLink:
        """El `DownloadLink` nativo: una instantánea del fichero en S3 que vale
        hasta que caduca la URL; `use_signature` se ignora como en `upload_url`."""
        return self._native.download_url(
            path, user=user, use_signature_expiration=use_signature_expiration
        )

    def get_host(self, port: int) -> HostAccess:
        """`HostAccess` nativo: un `str` con el hostname más `.headers` del proxy."""
        return self._native.get_host(port)

    # -------------------------------------------------------------------- code

    def run_code(
        self,
        code: str,
        language: str | None = None,
        context: ContextLike | None = None,
        on_stdout: StdoutCallback | None = None,
        on_stderr: StdoutCallback | None = None,
        on_result: ResultCallback | None = None,
        on_error: ErrorCallback | None = None,
        envs: Mapping[str, str] | None = None,
        timeout: float | None = None,
        request_timeout: float | None = None,
    ) -> Execution:
        """`run_code` de E2B: `language` `python`, `bash`, `javascript` o
        `typescript` (alias `js`/`ts`; `None` es Python) viaja al core, que
        decide en el agente si la imagen lo tiene. bash, javascript y
        typescript sólo existen en `rayito-base-poly` (javascript y typescript
        con el kernel de Deno, que arranca en la primera celda); en otra
        imagen el `UNIMPLEMENTED` del agente es `UnimplementedError` con la
        excepción nativa como `__cause__`, y cualquier otro error nativo se
        propaga tal cual. `r`, `java` y el resto son `UnimplementedError` sin
        tocar el agente. `timeout=None` es el nativo de 300 s."""
        canonical = normalized_language_or_unimplemented(language, "run_code")
        kwargs: dict[str, Any] = {
            "language": canonical,
            "context": context,
            "on_stdout": on_stdout,
            "on_stderr": on_stderr,
            "on_result": on_result,
            "on_error": on_error,
            "envs": envs,
            "request_timeout": request_timeout,
        }
        if timeout is not None:
            kwargs["timeout"] = timeout
        try:
            return self._native.run_code(code, **kwargs)
        except InvalidArgumentException as exc:
            mapped = unimplemented_language(exc, "run_code", language)
            if mapped is None:
                raise
            raise mapped from exc

    def create_code_context(
        self,
        cwd: str | None = None,
        language: str | None = None,
        request_timeout: float | None = None,
    ) -> CodeContext:
        """`create_code_context` de E2B con el mismo contrato de `language`
        que `run_code`: un kernel que la imagen no trae es
        `UnimplementedError` desde el `UNIMPLEMENTED` del agente."""
        canonical = normalized_language_or_unimplemented(language, "create_code_context")
        try:
            return self._native.create_code_context(
                cwd=cwd, language=canonical, request_timeout=request_timeout
            )
        except InvalidArgumentException as exc:
            mapped = unimplemented_language(exc, "create_code_context", language)
            if mapped is None:
                raise
            raise mapped from exc

    def list_code_contexts(
        self, request_timeout: float | None = None
    ) -> builtins.list[CodeContext]:
        return self._native.list_code_contexts(request_timeout=request_timeout)

    def remove_code_context(
        self, context: CodeContext | str, request_timeout: float | None = None
    ) -> None:
        self._native.remove_code_context(context, request_timeout=request_timeout)

    def restart_code_context(
        self, context: CodeContext | str, request_timeout: float | None = None
    ) -> None:
        self._native.restart_code_context(context, request_timeout=request_timeout)

    # --------------------------------------------------------- context manager

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.kill()

    def __repr__(self) -> str:
        return f"e2b.Sandbox(sandbox_id={self.sandbox_id!r})"


class Commands:
    """`sbx.commands` con las firmas posicionales de E2B 2.x sobre el
    `Commands` nativo; devuelve el `CommandResult`/`CommandHandle` nativo
    (`handle.wait(on_stdout=...)` funciona). El `tag=` nativo sigue en
    `sbx.native.commands`."""

    def __init__(self, native: NativeCommands) -> None:
        self._native = native

    @property
    def native(self) -> NativeCommands:
        return self._native

    @overload
    def run(
        self,
        cmd: str,
        background: Literal[False] | None = None,
        envs: Mapping[str, str] | None = None,
        user: str | None = None,
        cwd: str | None = None,
        on_stdout: OutputCallback | None = None,
        on_stderr: OutputCallback | None = None,
        stdin: bool | None = None,
        timeout: float | None = E2B_COMMAND_TIMEOUT_SECONDS,
        request_timeout: float | None = None,
    ) -> CommandResult: ...

    @overload
    def run(
        self,
        cmd: str,
        background: Literal[True],
        envs: Mapping[str, str] | None = None,
        user: str | None = None,
        cwd: str | None = None,
        on_stdout: OutputCallback | None = None,
        on_stderr: OutputCallback | None = None,
        stdin: bool | None = None,
        timeout: float | None = E2B_COMMAND_TIMEOUT_SECONDS,
        request_timeout: float | None = None,
    ) -> CommandHandle: ...

    def run(
        self,
        cmd: str,
        background: bool | None = None,
        envs: Mapping[str, str] | None = None,
        user: str | None = None,
        cwd: str | None = None,
        on_stdout: OutputCallback | None = None,
        on_stderr: OutputCallback | None = None,
        stdin: bool | None = None,
        timeout: float | None = E2B_COMMAND_TIMEOUT_SECONDS,
        request_timeout: float | None = None,
    ) -> CommandResult | CommandHandle:
        """`background=None` y `stdin=None` son `False`, como en E2B."""
        return self._native.run(
            cmd,
            background=native_background(background),
            envs=envs,
            user=user,
            cwd=cwd,
            on_stdout=on_stdout,
            on_stderr=on_stderr,
            stdin=native_stdin(stdin),
            timeout=timeout,
            request_timeout=request_timeout,
        )

    def connect(
        self,
        pid: int,
        timeout: float | None = E2B_COMMAND_TIMEOUT_SECONDS,
        request_timeout: float | None = None,
    ) -> CommandHandle:
        return self._native.connect(pid, timeout=timeout, request_timeout=request_timeout)

    def list(self, request_timeout: float | None = None) -> builtins.list[ProcessInfo]:
        return self._native.list(request_timeout=request_timeout)

    def kill(self, pid: int, request_timeout: float | None = None) -> bool:
        return self._native.kill(pid, request_timeout=request_timeout)

    def send_stdin(self, pid: int, data: str | bytes, request_timeout: float | None = None) -> None:
        self._native.send_stdin(pid, data, request_timeout=request_timeout)

    def close_stdin(self, pid: int, request_timeout: float | None = None) -> None:
        self._native.close_stdin(pid, request_timeout=request_timeout)


class Filesystem:
    """`sbx.files` con las firmas de E2B 2.x sobre el `Filesystem` nativo."""

    def __init__(self, native: NativeFilesystem) -> None:
        self._native = native

    @overload
    def write(
        self,
        path: str,
        data: str | bytes | IO[bytes] | IO[str],
        user: str | None = None,
        request_timeout: float | None = None,
        *,
        gzip: bool = False,
        metadata: Mapping[str, str] | None = None,
        use_octet_stream: bool = False,
    ) -> EntryInfo: ...

    @overload
    def write(
        self,
        path: Sequence[WriteEntry],
        data: None = None,
        user: str | None = None,
        request_timeout: float | None = None,
        *,
        gzip: bool = False,
        metadata: Mapping[str, str] | None = None,
        use_octet_stream: bool = False,
    ) -> builtins.list[EntryInfo]: ...

    def write(
        self,
        path: str | Sequence[WriteEntry],
        data: str | bytes | IO[bytes] | IO[str] | None = None,
        user: str | None = None,
        request_timeout: float | None = None,
        *,
        gzip: bool = False,
        metadata: Mapping[str, str] | None = None,
        use_octet_stream: bool = False,
    ) -> EntryInfo | builtins.list[EntryInfo]:
        """`write(path, data)` → `WriteInfo`; `write([WriteEntry, ...])` → lista.
        `gzip`, `metadata` y `use_octet_stream` son los nativos."""
        if isinstance(path, str):
            if data is None:
                raise TypeError("write(path, data): falta data")
            return self._native.write(
                path,
                data,
                user=user,
                gzip=gzip,
                metadata=metadata,
                use_octet_stream=use_octet_stream,
                request_timeout=request_timeout,
            )
        return self.write_files(
            path,
            user,
            request_timeout,
            gzip=gzip,
            metadata=metadata,
            use_octet_stream=use_octet_stream,
        )

    def write_files(
        self,
        files: Sequence[WriteEntry],
        user: str | None = None,
        request_timeout: float | None = None,
        *,
        gzip: bool = False,
        metadata: Mapping[str, str] | None = None,
        use_octet_stream: bool = False,
    ) -> builtins.list[EntryInfo]:
        """N ficheros en un solo stream `Write`; devuelve sus `WriteInfo` en orden."""
        return self._native.write_files(
            files,
            user=user,
            gzip=gzip,
            metadata=metadata,
            use_octet_stream=use_octet_stream,
            request_timeout=request_timeout,
        )

    @overload
    def read(
        self,
        path: str,
        format: Literal["text"] = "text",
        user: str | None = None,
        request_timeout: float | None = None,
        *,
        gzip: bool = False,
        stream_idle_timeout: float | None = None,
    ) -> str: ...

    @overload
    def read(
        self,
        path: str,
        format: Literal["bytes"],
        user: str | None = None,
        request_timeout: float | None = None,
        *,
        gzip: bool = False,
        stream_idle_timeout: float | None = None,
    ) -> bytes: ...

    @overload
    def read(
        self,
        path: str,
        format: Literal["stream"],
        user: str | None = None,
        request_timeout: float | None = None,
        *,
        gzip: bool = False,
        stream_idle_timeout: float | None = None,
    ) -> Iterator[bytes]: ...

    def read(
        self,
        path: str,
        format: Literal["text", "bytes", "stream"] = "text",
        user: str | None = None,
        request_timeout: float | None = None,
        *,
        gzip: bool = False,
        stream_idle_timeout: float | None = None,
    ) -> str | bytes | Iterator[bytes]:
        return self._native.read(
            path,
            format=format,
            gzip=gzip,
            stream_idle_timeout=stream_idle_timeout,
            user=user,
            request_timeout=request_timeout,
        )

    def list(
        self,
        path: str,
        depth: int = 1,
        user: str | None = None,
        request_timeout: float | None = None,
    ) -> builtins.list[EntryInfo]:
        return self._native.list(path, depth=depth, user=user, request_timeout=request_timeout)

    def exists(
        self, path: str, user: str | None = None, request_timeout: float | None = None
    ) -> bool:
        return self._native.exists(path, user=user, request_timeout=request_timeout)

    def get_info(
        self, path: str, user: str | None = None, request_timeout: float | None = None
    ) -> EntryInfo:
        return self._native.get_info(path, user=user, request_timeout=request_timeout)

    def remove(
        self, path: str, user: str | None = None, request_timeout: float | None = None
    ) -> None:
        self._native.remove(path, user=user, request_timeout=request_timeout)

    def rename(
        self,
        old_path: str,
        new_path: str,
        user: str | None = None,
        request_timeout: float | None = None,
    ) -> EntryInfo:
        return self._native.rename(old_path, new_path, user=user, request_timeout=request_timeout)

    def make_dir(
        self, path: str, user: str | None = None, request_timeout: float | None = None
    ) -> bool:
        return self._native.make_dir(path, user=user, request_timeout=request_timeout)

    def watch_dir(
        self,
        path: str,
        user: str | None = None,
        request_timeout: float | None = None,
        recursive: bool = False,
        include_entry: bool = False,
        allow_network_mounts: bool = False,
        *,
        on_event: EventCallback | None = None,
        on_exit: ExitCallback | None = None,
        timeout: float | None = None,
    ) -> WatchHandle:
        """`watch_dir` síncrono de E2B 2.x: sin `on_event` los eventos se
        recogen con `handle.get_new_events()`; vive hasta `stop()` salvo
        `timeout` (sus keepalives cuentan como actividad para la política de
        idle). `include_entry` llega al agente; `allow_network_mounts` se
        acepta sin efecto porque el sandbox no tiene montajes de red. Un
        invocable como segundo posicional (forma 1.x) es
        `InvalidArgumentException`."""
        reject_callable_in_user_slot(user, "watch_dir")
        return self._native.watch_dir(
            path,
            on_event=on_event,
            on_exit=on_exit,
            recursive=recursive,
            include_entry=include_entry,
            user=user,
            timeout=timeout,
            request_timeout=request_timeout,
        )


class Pty:
    """`sbx.pty` con `PtySize(rows, cols)` de E2B y las firmas síncronas 2.x."""

    def __init__(self, native: NativePty) -> None:
        self._native = native

    def create(
        self,
        size: PtySize,
        user: str | None = None,
        cwd: str | None = None,
        envs: Mapping[str, str] | None = None,
        timeout: float | None = E2B_PTY_TIMEOUT_SECONDS,
        request_timeout: float | None = None,
        *,
        on_data: PtyDataCallback | None = None,
    ) -> PtyHandle:
        """Abre una terminal; devuelve el `PtyHandle` nativo (un `CommandHandle`).
        Un invocable en el hueco de `user` (forma 1.x) es `InvalidArgumentException`."""
        reject_callable_in_user_slot(user, "pty.create")
        return self._native.create(
            size=pty_size_to_native(size),
            user=user,
            cwd=cwd,
            envs=envs,
            on_data=on_data,
            timeout=timeout,
            request_timeout=request_timeout,
        )

    def connect(
        self,
        pid: int,
        timeout: float | None = E2B_PTY_TIMEOUT_SECONDS,
        request_timeout: float | None = None,
        *,
        on_data: PtyDataCallback | None = None,
    ) -> PtyHandle:
        """Se reengancha a una terminal viva y recibe sólo la salida nueva."""
        return self._native.connect(
            pid, from_seq=0, on_data=on_data, timeout=timeout, request_timeout=request_timeout
        )

    def send_stdin(self, pid: int, data: bytes, request_timeout: float | None = None) -> None:
        self._native.send_input(pid, data, request_timeout=request_timeout)

    def resize(self, pid: int, size: PtySize, request_timeout: float | None = None) -> None:
        self._native.resize(pid, pty_size_to_native(size), request_timeout=request_timeout)

    def kill(self, pid: int, request_timeout: float | None = None) -> bool:
        return self._native.kill(pid, request_timeout=request_timeout)
