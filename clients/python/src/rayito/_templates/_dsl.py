"""`Template`: el builder fluido e inmutable de m15-templates (DSL de E2B v2,
investigación §3.1 y §3.6). Cada método devuelve un `Template` nuevo (nunca
muta `self`): encadenar `.pip_install(...).copy(...)` construye un
`TemplateSpec` paso a paso sin que dos ramas de un mismo builder compartan
estado. Puro: ninguna llamada de red, ningún fichero se abre aquí
(`_context.py`/`_artifact.py` los leen cuando `Template.build()` ensambla
el zip).

`AsyncTemplate` hereda el builder tal cual (los métodos fluidos devuelven
`Self`, así que `AsyncTemplate().pip_install(...)` sigue siendo un
`AsyncTemplate`) y sólo añade las variantes `async` de `build`/
`build_in_background`/`get_build_status`/`exists` (`_build_async.py`); el
resto de la superficie (DSL, `to_dockerfile`, `to_json`) es la misma
instancia pura, sin E/S, así que duplicarla en una rama async no aportaría
nada.
"""

from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path
from typing import TYPE_CHECKING, Any, Self

from rayito._images import (
    DEFAULT_BASE_IMAGE_NAME,
    DEFAULT_BUILD_TIMEOUT_SECONDS,
    DEFAULT_MEMORY_MIB,
)
from rayito._templates._instructions import (
    BASE_IMAGE_KIND,
    DEFAULT_TEMPLATE_USER,
    TEMPLATE_SPEC_VERSION,
    BaseImageRef,
    CopyStep,
    EnvStep,
    ReadyPoll,
    RunStep,
    StartSpec,
    TemplateSpec,
    UserStep,
    WorkdirStep,
)
from rayito._templates._ready_cmds import ReadyCommand
from rayito.exceptions import InvalidArgumentException, UnimplementedError

if TYPE_CHECKING:
    from rayito._templates._models import BuildHandle, BuildInfo, BuildStatus

#: `pip_install()`: rayito-base (al2023-minimal) no tiene `pip` en el PATH,
#: sólo `python3 -m pip` (Q116: `RUN pip install` sale con 127); mismas
#: banderas que las capas de `image/Dockerfile`.
PIP_INSTALL_COMMAND = "python3 -m pip install --no-cache-dir --break-system-packages"

_UNSUPPORTED_BASE_REASON = (
    "Template.{method}() necesita inyectar rayd y sus hooks en la imagen de base; en 0.6 sólo "
    "from_base_image() sabe componer eso (siempre sobre un zip de codeArtifact ya publicado con "
    "rayito image publish). Para una imagen externa, publícala primero con rayito image publish "
    "sobre un Dockerfile `FROM <esa imagen>@sha256:...`."
)


class Template:
    """Builder inmutable de un `TemplateSpec`. `Template()` crea uno vacío
    (sin imagen base): llama a `from_base_image()` antes de compilar."""

    __slots__ = ("_spec",)

    def __init__(self, spec: TemplateSpec | None = None) -> None:
        self._spec = spec if spec is not None else TemplateSpec()

    @property
    def spec(self) -> TemplateSpec:
        return self._spec

    def _with(self, spec: TemplateSpec) -> Self:
        return self.__class__(spec)

    # -- imagen base -----------------------------------------------------

    def from_base_image(
        self, name: str = DEFAULT_BASE_IMAGE_NAME, *, version: str | None = None
    ) -> Self:
        """Parte del zip de `codeArtifact` de `name` (ya publicada con
        `rayito image publish`): `Template.build()` lo descarga
        (`s3:GetObject`), le añade las instrucciones de cable compiladas y
        vuelve a subir el resultado. `version` fija una versión en vez de
        la última `ACTIVE`."""
        if not name:
            raise InvalidArgumentException(
                "from_base_image: el nombre de la imagen no puede ser vacío"
            )
        return self._with(self._spec.with_base(BaseImageRef(BASE_IMAGE_KIND, name, version)))

    def from_image(self, *_args: Any, **_kwargs: Any) -> Self:
        raise UnimplementedError(
            "Template.from_image", _UNSUPPORTED_BASE_REASON.format(method="from_image")
        )

    def from_template(self, *_args: Any, **_kwargs: Any) -> Self:
        raise UnimplementedError(
            "Template.from_template", _UNSUPPORTED_BASE_REASON.format(method="from_template")
        )

    def from_dockerfile(self, *_args: Any, **_kwargs: Any) -> Self:
        raise UnimplementedError(
            "Template.from_dockerfile",
            "el compilador de 0.6 construye el Dockerfile desde el DSL; parsear uno existente "
            "llega en un cambio posterior (ver docs-delta.md de m15-templates)",
        )

    def from_gcp_registry(self, *_args: Any, **_kwargs: Any) -> Self:
        raise UnimplementedError(
            "Template.from_gcp_registry",
            "Rayito publica sobre Lambda MicroVMs (AWS): no hay un análogo de GCP Artifact "
            "Registry que `create-microvm-image` pueda usar",
        )

    # -- instrucciones de cable -------------------------------------------

    def copy(self, src: str, dst: str) -> Self:
        if not src or not dst:
            raise InvalidArgumentException("copy: src y dst no pueden ser vacíos")
        if not dst.startswith("/"):
            raise InvalidArgumentException(
                f"copy: dst debe ser una ruta absoluta en la imagen: {dst!r}"
            )
        return self._with(self._spec.with_step(CopyStep(src, dst)))

    def run_cmd(self, cmd: str) -> Self:
        if not cmd.strip():
            raise InvalidArgumentException("run_cmd: el comando no puede ser vacío")
        return self._with(self._spec.with_step(RunStep(cmd)))

    def pip_install(self, packages: str | list[str], *, extra_args: str = "") -> Self:
        names = [packages] if isinstance(packages, str) else list(packages)
        if not names:
            raise InvalidArgumentException("pip_install: la lista de paquetes no puede ser vacía")
        args = f" {extra_args}" if extra_args else ""
        return self.run_cmd(f"{PIP_INSTALL_COMMAND}{args} " + " ".join(names))

    def apt_install(self, *_args: Any, **_kwargs: Any) -> Self:
        raise UnimplementedError(
            "Template.apt_install",
            "rayito-base es Amazon Linux 2023 (dnf, no apt): usa run_cmd('dnf install -y ...')",
        )

    def set_envs(self, envs: dict[str, str]) -> Self:
        spec = self._spec
        for key, value in envs.items():
            if not key:
                raise InvalidArgumentException("set_envs: una clave no puede ser vacía")
            spec = spec.with_step(EnvStep(key, value))
        return self._with(spec)

    def workdir(self, path: str) -> Self:
        if not path.startswith("/"):
            raise InvalidArgumentException(f"workdir: debe ser una ruta absoluta: {path!r}")
        return self._with(self._spec.with_step(WorkdirStep(path)))

    def set_user(self, user: str) -> Self:
        if not user:
            raise InvalidArgumentException("set_user: el usuario no puede ser vacío")
        return self._with(self._spec.with_step(UserStep(user)))

    def skip_cache(self, *, skip_cache: bool = True) -> Self:
        """0.6 no tiene caché de capas propia (investigación §3.4): esto
        hace que `Template.build()`/`build_in_background()` se comporten
        como con `force=True` — envían un build nuevo aunque ya exista una
        versión con la misma configuración."""
        return self._with(self._spec.with_skip_cache(skip_cache=skip_cache))

    # -- start / ready -----------------------------------------------------

    def set_start_cmd(
        self,
        start_cmd: str,
        ready_cmd: ReadyCommand | str | None = None,
        *,
        user: str = DEFAULT_TEMPLATE_USER,
        workdir: str | None = None,
        envs: dict[str, str] | None = None,
    ) -> Self:
        """Hornea `/etc/rayito/template.json` (`rayito.template/1`). Al
        arrancar, un `rayd` 0.6 o posterior lo lee y, antes del `/ready` del
        build (el snapshot ya lo lleva en marcha), lanza `start_cmd` como
        proceso gestionado (visible en `commands.list()`); si hay
        `ready_cmd`, `/ready` responde 503 hasta que el comando sale con 0, y
        500 al agotarse
        `ReadyPoll.timeout_seconds` (`wait_for_port`/`wait_for_url`/
        `wait_for_process`/`wait_for_file` en `_ready_cmds.py`; una cadena
        cruda se sondea con la cadencia por defecto, 0,5 s durante 60 s).
        Requiere que la imagen base se haya publicado con `rayd` 0.6 o
        posterior: un `rayd` anterior ignora el fichero."""
        if not start_cmd.strip():
            raise InvalidArgumentException("set_start_cmd: start_cmd no puede ser vacío")
        poll: ReadyPoll | None
        if ready_cmd is None:
            ready_cmd_text, poll = None, None
        elif isinstance(ready_cmd, str):
            ready_cmd_text, poll = ready_cmd, None
        else:
            ready_cmd_text, poll = ready_cmd.cmd, ready_cmd.poll
        start = StartSpec(
            start_cmd=start_cmd,
            ready_cmd=ready_cmd_text,
            user=user,
            workdir=workdir,
            envs=tuple(sorted((envs or {}).items())),
            ready_poll=poll,
        )
        return self._with(self._spec.with_start(start))

    # -- inspección ---------------------------------------------------------

    def to_dockerfile(self) -> str:
        """El Dockerfile real que `Template.build()` sube (investigación
        §3.6: `to_dockerfile()` ya existe en E2B)."""
        from rayito._templates._dockerfile import render_appended_layer

        return render_appended_layer(self._spec)

    def to_json(self) -> str:
        """Serialización estable del `TemplateSpec` (depurar/diffear un
        template entre builds; no es lo que `rayd` lee: eso es
        `/etc/rayito/template.json`, un subconjunto sólo del `StartSpec`)."""
        return json.dumps(_spec_to_dict(self._spec), indent=2, sort_keys=True)

    # -- construcción (delegada: ver _build.py / _build_async.py) ----------

    @classmethod
    def build(
        cls,
        template: Template,
        name: str,
        *,
        bucket: str,
        memory_mb: int = DEFAULT_MEMORY_MIB,
        cpu_count: int | None = None,
        force: bool = False,
        timeout: float = DEFAULT_BUILD_TIMEOUT_SECONDS,
        base_image_version: str | None = None,
        build_role_arn: str | None = None,
        on_build_logs: Any = None,
        region: str | None = None,
        session: Any = None,
        context_dir: Path | None = None,
    ) -> BuildInfo:
        from rayito._templates._build import build as _build

        return _build(
            template,
            name,
            bucket=bucket,
            memory_mb=memory_mb,
            cpu_count=cpu_count,
            force=force,
            timeout=timeout,
            base_image_version=base_image_version,
            build_role_arn=build_role_arn,
            on_build_logs=on_build_logs,
            region=region,
            session=session,
            context_dir=context_dir,
        )

    @classmethod
    def build_in_background(
        cls,
        template: Template,
        name: str,
        *,
        bucket: str,
        memory_mb: int = DEFAULT_MEMORY_MIB,
        cpu_count: int | None = None,
        force: bool = False,
        base_image_version: str | None = None,
        build_role_arn: str | None = None,
        region: str | None = None,
        session: Any = None,
        context_dir: Path | None = None,
    ) -> BuildHandle:
        from rayito._templates._build import build_in_background as _build_in_background

        return _build_in_background(
            template,
            name,
            bucket=bucket,
            memory_mb=memory_mb,
            cpu_count=cpu_count,
            force=force,
            base_image_version=base_image_version,
            build_role_arn=build_role_arn,
            region=region,
            session=session,
            context_dir=context_dir,
        )

    @classmethod
    def get_build_status(cls, handle: BuildHandle) -> BuildStatus:
        from rayito._templates._build import get_build_status as _get_build_status

        return _get_build_status(handle)

    @classmethod
    def exists(cls, name: str, *, region: str | None = None, session: Any = None) -> bool:
        from rayito._templates._build import template_exists as _template_exists

        return _template_exists(name, region=region, session=session)


class AsyncTemplate(Template):
    """`Template` con `build`/`build_in_background`/`get_build_status`/
    `exists` en versión `async` (`asyncio.to_thread`, como el resto del SDK
    async: ver `sandbox_async/*`). El resto de la superficie (DSL,
    `to_dockerfile`, `to_json`) es pura y se hereda tal cual."""

    @classmethod
    async def build(  # type: ignore[override]
        cls,
        template: Template,
        name: str,
        *,
        bucket: str,
        memory_mb: int = DEFAULT_MEMORY_MIB,
        cpu_count: int | None = None,
        force: bool = False,
        timeout: float = DEFAULT_BUILD_TIMEOUT_SECONDS,
        base_image_version: str | None = None,
        build_role_arn: str | None = None,
        on_build_logs: Any = None,
        region: str | None = None,
        session: Any = None,
        context_dir: Path | None = None,
    ) -> BuildInfo:
        from rayito._templates._build_async import build as _build

        return await _build(
            template,
            name,
            bucket=bucket,
            memory_mb=memory_mb,
            cpu_count=cpu_count,
            force=force,
            timeout=timeout,
            base_image_version=base_image_version,
            build_role_arn=build_role_arn,
            on_build_logs=on_build_logs,
            region=region,
            session=session,
            context_dir=context_dir,
        )

    @classmethod
    async def build_in_background(  # type: ignore[override]
        cls,
        template: Template,
        name: str,
        *,
        bucket: str,
        memory_mb: int = DEFAULT_MEMORY_MIB,
        cpu_count: int | None = None,
        force: bool = False,
        base_image_version: str | None = None,
        build_role_arn: str | None = None,
        region: str | None = None,
        session: Any = None,
        context_dir: Path | None = None,
    ) -> BuildHandle:
        from rayito._templates._build_async import build_in_background as _build_in_background

        return await _build_in_background(
            template,
            name,
            bucket=bucket,
            memory_mb=memory_mb,
            cpu_count=cpu_count,
            force=force,
            base_image_version=base_image_version,
            build_role_arn=build_role_arn,
            region=region,
            session=session,
            context_dir=context_dir,
        )

    @classmethod
    async def get_build_status(cls, handle: BuildHandle) -> BuildStatus:  # type: ignore[override]
        from rayito._templates._build_async import get_build_status as _get_build_status

        return await _get_build_status(handle)

    @classmethod
    async def exists(  # type: ignore[override]
        cls, name: str, *, region: str | None = None, session: Any = None
    ) -> bool:
        from rayito._templates._build_async import template_exists as _template_exists

        return await _template_exists(name, region=region, session=session)


def _spec_to_dict(spec: TemplateSpec) -> dict[str, Any]:
    def step_to_dict(step: Any) -> dict[str, Any]:
        return {"kind": type(step).__name__, **asdict(step)}

    return {
        "version": TEMPLATE_SPEC_VERSION,
        "base": asdict(spec.base) if spec.base else None,
        "steps": [step_to_dict(step) for step in spec.steps],
        "start": asdict(spec.start) if spec.start else None,
        "skip_cache": spec.skip_cache,
    }
