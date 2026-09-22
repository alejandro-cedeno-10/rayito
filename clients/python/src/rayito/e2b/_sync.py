"""`rayito.e2b.Sandbox`: la superficie de `e2b_code_interpreter.Sandbox` /
`e2b.Sandbox` (SDK 1.x) sobre un `rayito.Sandbox` nativo por composición.

El constructor crea el sandbox (E2B v1) o se conecta si recibe
`sandbox_id`; `commands` es el `Commands` nativo tal cual; `files` y `pty`
son wrappers finos que adaptan las firmas de E2B (`write` con dos formas,
`watch_dir` con `on_event` posicional y 60 s, `PtySize(rows, cols)`). Todo
lo que Lambda MicroVMs no puede hacer lanza `UnimplementedError` antes de
tocar AWS o el agente. El nativo queda accesible en `sbx.native`.
"""

from __future__ import annotations

import builtins
import warnings
from collections.abc import Iterator, Mapping, Sequence
from types import TracebackType
from typing import IO, Any, Literal, Self, overload

from rayito import Sandbox as NativeSandbox
from rayito._code_base import ContextLike, ErrorCallback, ResultCallback, StdoutCallback
from rayito._filesystem_base import EventCallback, ExitCallback
from rayito._models import CodeContext, EntryInfo, Execution, HostAccess, WriteEntry
from rayito._pty_base import PtyDataCallback
from rayito._sandbox_base import (
    DEFAULT_READY_TIMEOUT_SECONDS,
    DEFAULT_RECONNECT_TIMEOUT_SECONDS,
    LoggingOption,
    PortLike,
    class_method_variant,
)
from rayito.e2b._compat import (
    create_only_kwargs_given,
    ignored_kwarg_warnings,
    info_from_native,
    map_create_kwargs,
    metrics_from_native,
    normalized_language_or_unimplemented,
    pty_size_to_native,
    states_for,
    unimplemented,
)
from rayito.e2b._models import (
    PtySize,
    SandboxInfo,
    SandboxMetrics,
    SandboxPaginator,
    SandboxQuery,
    SandboxState,
)
from rayito.e2b.exceptions import RayitoCompatWarning
from rayito.sandbox_sync.commands import Commands
from rayito.sandbox_sync.filesystem import Filesystem as NativeFilesystem
from rayito.sandbox_sync.filesystem import WatchHandle
from rayito.sandbox_sync.pty import Pty as NativePty
from rayito.sandbox_sync.pty import PtyHandle

E2B_WATCH_TIMEOUT_SECONDS = 60
E2B_PTY_TIMEOUT_SECONDS = 60
WARN_STACKLEVEL = 3

SET_TIMEOUT_REASON = (
    "la vida de un MicroVM es inmutable: no existe UpdateMicrovm (ADR-007); usa "
    "rayito.Sandbox.reincarnate() (checkpoint en S3 + VM nueva, ADR-009)"
)
SIGNED_URL_REASON = (
    "el proxy de Lambda MicroVMs exige cabeceras firmadas por petición; usa "
    "files.write/read o get_host(port).headers"
)
METRICS_RANGE_REASON = "no hay historial: Metrics es una instantánea procfs en vivo"
CLASS_METRICS_REASON = "necesita el access token del sandbox: usa connect()"
CONNECTION_CONFIG_REASON = "no hay api_key ni domain: la conexión es un JWE por sandbox"
NEXT_TOKEN_REASON = "Rayito pagina list-microvms por dentro; no hay cursor reanudable"
BETA_CREATE_REASON = "auto_pause, network y mcp no tienen primitiva en Lambda MicroVMs"
IGNORED_ON_CONNECT_REASON = "Sandbox(sandbox_id=...) se conecta y no aplica: "


def emit_warnings(messages: Sequence[str]) -> None:
    for message in messages:
        warnings.warn(message, RayitoCompatWarning, stacklevel=WARN_STACKLEVEL)


class Sandbox:
    """Drop-in de `e2b_code_interpreter.Sandbox` / `e2b.Sandbox`."""

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
    ) -> None:
        """Crea el sandbox (`timeout` 300 s por defecto, sin auto-pausa) o, con
        `sandbox_id`, se conecta (comportamiento E2B v1). `api_key`, `domain`,
        `debug`, `proxy` y `secure=False` se ignoran con un `RayitoCompatWarning`
        cada uno; `allow_internet_access=False` es `UnimplementedError` (Q44: sin
        conector de egress el MicroVM sigue saliendo a internet)."""
        if _native is not None:
            self._bind(_native)
            return
        connect_kwargs: dict[str, Any] = {
            "api_key": api_key,
            "domain": domain,
            "debug": debug,
            "proxy": proxy,
            "access_token": access_token,
            "region": region,
            "session": session,
            "request_timeout": request_timeout,
            "ready_timeout": ready_timeout,
            "reconnect_timeout": reconnect_timeout,
            "control_plane": control_plane,
            "transport": transport,
        }
        if sandbox_id is not None:
            self._warn_create_only_kwargs(
                template=template,
                timeout=timeout,
                metadata=metadata,
                envs=envs,
                template_version=template_version,
                execution_role_arn=execution_role_arn,
                allowed_ports=allowed_ports,
                ingress=ingress,
                allow_internet_access=None if allow_internet_access else False,
                secure=None if secure else False,
            )
            self._bind(self._native_connect(sandbox_id, **connect_kwargs))
            return
        mapping = map_create_kwargs(
            template=template,
            timeout=timeout,
            metadata=metadata,
            envs=envs,
            api_key=api_key,
            domain=domain,
            debug=debug,
            request_timeout=request_timeout,
            proxy=proxy,
            secure=secure,
            allow_internet_access=allow_internet_access,
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
        )
        emit_warnings(mapping.warnings)
        self._bind(NativeSandbox.create(**mapping.native_kwargs))

    def _bind(self, native: NativeSandbox) -> None:
        self._native = native
        self.commands: Commands = native.commands
        self.files = Filesystem(native.files)
        self.pty = Pty(native.pty)

    @staticmethod
    def _warn_create_only_kwargs(**kwargs: Any) -> None:
        ignored = create_only_kwargs_given(**kwargs)
        if ignored:
            emit_warnings((IGNORED_ON_CONNECT_REASON + ", ".join(ignored),))

    @staticmethod
    def _native_connect(
        sandbox_id: str,
        *,
        api_key: str | None,
        domain: str | None,
        debug: bool,
        proxy: str | None,
        access_token: str | None,
        region: str | None,
        session: Any | None,
        request_timeout: float | None,
        ready_timeout: float,
        reconnect_timeout: float,
        control_plane: Any | None,
        transport: Any | None,
    ) -> NativeSandbox:
        emit_warnings(
            ignored_kwarg_warnings(
                api_key=api_key, domain=domain, debug=debug, proxy=proxy, secure=True
            )
        )
        kwargs: dict[str, Any] = {
            "access_token": access_token,
            "region": region,
            "session": session,
            "ready_timeout": ready_timeout,
            "reconnect_timeout": reconnect_timeout,
            "control_plane": control_plane,
            "transport": transport,
        }
        if request_timeout is not None:
            kwargs["request_timeout"] = request_timeout
        return NativeSandbox.connect(sandbox_id, **kwargs)

    # ------------------------------------------------------------ classmethods

    @classmethod
    def create(cls, *args: Any, **kwargs: Any) -> Self:
        """`Sandbox.create(...)` (E2B ≥ 1.5): los mismos kwargs que el constructor."""
        return cls(*args, **kwargs)

    @classmethod
    def connect(
        cls,
        sandbox_id: str,
        *,
        api_key: str | None = None,
        domain: str | None = None,
        debug: bool = False,
        request_timeout: float | None = None,
        proxy: str | None = None,
        access_token: str | None = None,
        region: str | None = None,
        session: Any | None = None,
        ready_timeout: float = DEFAULT_READY_TIMEOUT_SECONDS,
        reconnect_timeout: float = DEFAULT_RECONNECT_TIMEOUT_SECONDS,
        control_plane: Any | None = None,
        transport: Any | None = None,
    ) -> Self:
        """Se engancha a un sandbox existente (y lo reanuda si estaba pausado).
        Necesita el `access_token` del sandbox o `RAYITO_ACCESS_TOKEN`."""
        native = cls._native_connect(
            sandbox_id,
            api_key=api_key,
            domain=domain,
            debug=debug,
            proxy=proxy,
            access_token=access_token,
            region=region,
            session=session,
            request_timeout=request_timeout,
            ready_timeout=ready_timeout,
            reconnect_timeout=reconnect_timeout,
            control_plane=control_plane,
            transport=transport,
        )
        return cls(_native=native)

    @classmethod
    def beta_create(
        cls,
        *args: Any,
        auto_pause: bool | None = None,
        network: Any | None = None,
        mcp: Any | None = None,
        **kwargs: Any,
    ) -> Self:
        """`Sandbox.beta_create`: igual que `create` salvo `auto_pause`,
        `network` y `mcp`, que no tienen equivalente."""
        if auto_pause is not None or network is not None or mcp is not None:
            raise unimplemented("beta_create(auto_pause=, network=, mcp=)", BETA_CREATE_REASON)
        return cls(*args, **kwargs)

    @classmethod
    def list(
        cls,
        *,
        api_key: str | None = None,
        query: SandboxQuery | None = None,
        state: Sequence[SandboxState] | None = None,
        limit: int | None = None,
        next_token: str | None = None,
        domain: str | None = None,
        debug: bool = False,
        request_timeout: float | None = None,
        proxy: str | None = None,
        template: str | None = None,
        template_version: str | None = None,
        region: str | None = None,
        session: Any | None = None,
        control_plane: Any | None = None,
        transport: Any | None = None,
    ) -> SandboxPaginator:
        """`SandboxPaginator` perezoso sobre `rayito.Sandbox.list`. Con
        `query.metadata` el filtro es O(n) sobre los sandboxes `RUNNING`
        (un `Health` por sandbox); ver `rayito.Sandbox.list`."""
        if next_token is not None:
            raise unimplemented("list(next_token=...)", NEXT_TOKEN_REASON)
        states = states_for(state, query)
        emit_warnings(
            ignored_kwarg_warnings(
                api_key=api_key, domain=domain, debug=debug, proxy=proxy, secure=True
            )
        )
        kwargs: dict[str, Any] = {
            "template": template,
            "template_version": template_version,
            "states": states,
            "metadata": None if query is None else query.metadata,
            "region": region,
            "session": session,
            "control_plane": control_plane,
            "transport": transport,
        }
        if request_timeout is not None:
            kwargs["request_timeout"] = request_timeout
        items = NativeSandbox.list(**kwargs)
        return SandboxPaginator((info_from_native(item) for item in items), limit=limit)

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
    def connection_config(self) -> Any:
        raise unimplemented("connection_config", CONNECTION_CONFIG_REASON)

    # --------------------------------------------------------------- lifecycle

    def is_running(self) -> bool:
        return self._native.is_running()

    @class_method_variant("_class_kill")
    def kill(self) -> bool:
        return bool(self._native.kill())

    @classmethod
    def _class_kill(cls, sandbox_id: str, **kwargs: Any) -> bool:
        return bool(NativeSandbox.kill(sandbox_id, **kwargs))

    @class_method_variant("_class_get_info")
    def get_info(self, request_timeout: float | None = None) -> SandboxInfo:
        """`SandboxInfo` de E2B con los metadatos del último `Health`."""
        return info_from_native(self._native.get_info())

    @classmethod
    def _class_get_info(
        cls, sandbox_id: str, request_timeout: float | None = None, **kwargs: Any
    ) -> SandboxInfo:
        """`Sandbox.get_info(sandbox_id)`: lee los metadatos con un `Health`
        cuando el sandbox está `RUNNING`; un sandbox terminado es
        `NotFoundException` (el 404 de E2B)."""
        if request_timeout is not None:
            kwargs["request_timeout"] = request_timeout
        return info_from_native(NativeSandbox.get_info(sandbox_id, **kwargs))

    @class_method_variant("_class_set_timeout")
    def set_timeout(self, timeout: int, request_timeout: float | None = None) -> None:
        raise unimplemented("set_timeout", SET_TIMEOUT_REASON)

    @classmethod
    def _class_set_timeout(
        cls, sandbox_id: str, timeout: int, request_timeout: float | None = None, **kwargs: Any
    ) -> None:
        raise unimplemented("set_timeout", SET_TIMEOUT_REASON)

    @class_method_variant("_class_get_metrics")
    def get_metrics(
        self,
        start: int | None = None,
        end: int | None = None,
        request_timeout: float | None = None,
    ) -> builtins.list[SandboxMetrics]:
        """Una instantánea (`Metrics` procfs) con los nombres de E2B; sin rangos."""
        if start is not None or end is not None:
            raise unimplemented("get_metrics(start=, end=)", METRICS_RANGE_REASON)
        return [metrics_from_native(self._native.get_metrics(request_timeout=request_timeout))]

    @classmethod
    def _class_get_metrics(
        cls, sandbox_id: str, *args: Any, **kwargs: Any
    ) -> builtins.list[SandboxMetrics]:
        raise unimplemented("Sandbox.get_metrics(sandbox_id)", CLASS_METRICS_REASON)

    @class_method_variant("_class_pause")
    def pause(self) -> str:
        """`suspend-microvm` y espera a `SUSPENDED`; devuelve el id como E2B."""
        self._native.pause(wait=True)
        return self.sandbox_id

    @classmethod
    def _class_pause(cls, sandbox_id: str, **kwargs: Any) -> str:
        NativeSandbox.pause(sandbox_id, **kwargs)
        return sandbox_id

    beta_pause = pause

    def upload_url(
        self,
        path: str | None = None,
        user: str | None = None,
        use_signature: bool = False,
        use_signature_expiration: int | None = None,
    ) -> str:
        raise unimplemented("upload_url", SIGNED_URL_REASON)

    def download_url(
        self,
        path: str,
        user: str | None = None,
        use_signature: bool = False,
        use_signature_expiration: int | None = None,
    ) -> str:
        raise unimplemented("download_url", SIGNED_URL_REASON)

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
        """`run_code` de E2B: `language` `python`, `bash` o `javascript` (alias
        `js`; `None` es Python) viaja al core, que decide en el agente si la
        imagen lo tiene (`bash` sólo en `rayito-base-poly`; `javascript` es un
        nombre reservado que hoy ninguna imagen trae); `r`, `java` y el resto
        son `UnimplementedError`. `timeout=None` es el nativo de 300 s."""
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
        return self._native.run_code(code, **kwargs)

    def create_code_context(
        self,
        cwd: str | None = None,
        language: str | None = None,
        request_timeout: float | None = None,
    ) -> CodeContext:
        canonical = normalized_language_or_unimplemented(language, "create_code_context")
        return self._native.create_code_context(
            cwd=cwd, language=canonical, request_timeout=request_timeout
        )

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


class Filesystem:
    """`sbx.files` con las firmas de E2B sobre el `Filesystem` nativo."""

    def __init__(self, native: NativeFilesystem) -> None:
        self._native = native

    @overload
    def write(
        self,
        path: str,
        data: str | bytes | IO[bytes] | IO[str],
        user: str | None = None,
        request_timeout: float | None = None,
    ) -> EntryInfo: ...

    @overload
    def write(
        self,
        path: Sequence[WriteEntry],
        data: None = None,
        user: str | None = None,
        request_timeout: float | None = None,
    ) -> builtins.list[EntryInfo]: ...

    def write(
        self,
        path: str | Sequence[WriteEntry],
        data: str | bytes | IO[bytes] | IO[str] | None = None,
        user: str | None = None,
        request_timeout: float | None = None,
    ) -> EntryInfo | builtins.list[EntryInfo]:
        """`write(path, data)` → `WriteInfo`; `write([WriteEntry, ...])` → lista."""
        if isinstance(path, str):
            if data is None:
                raise TypeError("write(path, data): falta data")
            return self._native.write(path, data, user=user, request_timeout=request_timeout)
        return self._native.write_files(path, user=user, request_timeout=request_timeout)

    @overload
    def read(
        self,
        path: str,
        format: Literal["text"] = "text",
        user: str | None = None,
        request_timeout: float | None = None,
    ) -> str: ...

    @overload
    def read(
        self,
        path: str,
        format: Literal["bytes"],
        user: str | None = None,
        request_timeout: float | None = None,
    ) -> bytes: ...

    @overload
    def read(
        self,
        path: str,
        format: Literal["stream"],
        user: str | None = None,
        request_timeout: float | None = None,
    ) -> Iterator[bytes]: ...

    def read(
        self,
        path: str,
        format: Literal["text", "bytes", "stream"] = "text",
        user: str | None = None,
        request_timeout: float | None = None,
    ) -> str | bytes | Iterator[bytes]:
        return self._native.read(path, format=format, user=user, request_timeout=request_timeout)

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
        on_event: EventCallback | None = None,
        on_exit: ExitCallback | None = None,
        user: str | None = None,
        request_timeout: float | None = None,
        timeout: float | None = E2B_WATCH_TIMEOUT_SECONDS,
        recursive: bool = False,
    ) -> WatchHandle:
        """`watch_dir` de E2B: `on_event` posicional y 60 s de vida por defecto
        (termina en `TimeoutException`, como en E2B); `0`/`None` es ilimitado."""
        return self._native.watch_dir(
            path,
            on_event=on_event,
            on_exit=on_exit,
            recursive=recursive,
            user=user,
            timeout=timeout,
            request_timeout=request_timeout,
        )


class Pty:
    """`sbx.pty` con `PtySize(rows, cols)` de E2B y `send_stdin`."""

    def __init__(self, native: NativePty) -> None:
        self._native = native

    def create(
        self,
        size: PtySize,
        on_data: PtyDataCallback | None = None,
        user: str | None = None,
        cwd: str | None = None,
        envs: Mapping[str, str] | None = None,
        timeout: float | None = E2B_PTY_TIMEOUT_SECONDS,
        request_timeout: float | None = None,
    ) -> PtyHandle:
        """Abre una terminal; devuelve el `PtyHandle` nativo (un `CommandHandle`)."""
        return self._native.create(
            size=pty_size_to_native(size),
            user=user,
            cwd=cwd,
            envs=envs,
            on_data=on_data,
            timeout=timeout,
            request_timeout=request_timeout,
        )

    def send_stdin(self, pid: int, data: bytes, request_timeout: float | None = None) -> None:
        self._native.send_input(pid, data, request_timeout=request_timeout)

    def resize(self, pid: int, size: PtySize, request_timeout: float | None = None) -> None:
        self._native.resize(pid, pty_size_to_native(size), request_timeout=request_timeout)

    def kill(self, pid: int, request_timeout: float | None = None) -> bool:
        return self._native.kill(pid, request_timeout=request_timeout)
