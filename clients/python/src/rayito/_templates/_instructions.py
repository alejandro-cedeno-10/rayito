"""Dominio puro de `Template` (m15-templates): las cinco instrucciones que
llegan al cable (`docs/research/2026-10-e2b-out-of-scope.md` §3.1, igual que
`e2b/template/types.py`: COPY, ENV, RUN, WORKDIR, USER) más la referencia a
la imagen base y el `TemplateSpec` inmutable que agrupa todo. Nada aquí
importa `boto3`, abre un fichero o hace E/S: `_context.py` lee los ficheros
que `CopyStep` nombra y `_dockerfile.py`/`_artifact.py` convierten esto en
texto y bytes.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Final

#: Marca de versión del `/etc/rayito/template.json` que lee `rayd`
#: (investigación §3.5); cambia sólo si el esquema deja de ser compatible.
TEMPLATE_SPEC_VERSION: Final = "rayito.template/1"

#: Usuario por defecto del start/ready cmd (investigación §3.5: "user, def. 1000"),
#: el mismo uid que usa el resto de Rayito para el código del sandbox.
DEFAULT_TEMPLATE_USER: Final = "1000"

#: Único tipo de imagen base que `from_base_image()` sabe componer en 0.6:
#: el zip de `codeArtifact` publicado por `rayito image publish`
#: (investigación §3.4 punto 3: "`from_base_image()` asume que
#: `GetMicrovmImageVersion` devuelve un `codeArtifact.uri` utilizable").
#: `from_image()`/`from_template()` quedan fuera de alcance en 0.6 (ver
#: `docs-delta.md` y el ADR-022): no hay forma de inyectar `rayd` y sus
#: hooks en una imagen externa sin reconstruir la cadena de arranque
#: entera, así que lanzan `UnimplementedError` nombrando esta limitación.
BASE_IMAGE_KIND: Final = "rayito-base"


@dataclass(frozen=True, slots=True)
class BaseImageRef:
    """Qué imagen compone `from_base_image()`. `name` es el nombre o ARN de
    la imagen ya publicada (`rayito-base`, `rayito-base-caps`, ...);
    `version` fija una versión en vez de "la última ACTIVE" (por defecto
    `None`)."""

    kind: str
    name: str
    version: str | None = None


@dataclass(frozen=True, slots=True)
class CopyStep:
    """`COPY <src> <dst>`: `src` es una ruta relativa al contexto (bajo el
    directorio desde el que se llama a `Template.build`), `dst` una ruta
    absoluta en la imagen."""

    src: str
    dst: str


@dataclass(frozen=True, slots=True)
class EnvStep:
    """`ENV <key>=<value>`: horneado en la imagen y en todos sus
    snapshots. Nunca un secreto (investigación §3.7): `set_envs` no acepta
    nada que deba rotar."""

    key: str
    value: str


@dataclass(frozen=True, slots=True)
class RunStep:
    """`RUN <cmd>`, ejecutado con `/bin/sh -c`."""

    cmd: str


@dataclass(frozen=True, slots=True)
class WorkdirStep:
    """`WORKDIR <path>`: afecta a los `RunStep`/`CopyStep` posteriores y al
    `workdir` por defecto del start cmd."""

    path: str


@dataclass(frozen=True, slots=True)
class UserStep:
    """`USER <user>`: afecta a los `RunStep` posteriores. `DockerfileRenderer`
    siempre vuelve a `USER root` antes del `CMD` de `rayd` final
    (investigación §3.5: "`rayd` sigue siendo PID 1")."""

    user: str


#: Unión de las cinco instrucciones de cable; el orden en que aparecen en
#: `TemplateSpec.steps` es el orden en que se compilan.
WireStep = CopyStep | EnvStep | RunStep | WorkdirStep | UserStep


@dataclass(frozen=True, slots=True)
class ReadyPoll:
    """Cuánto y con qué cadencia `rayd` sondea `ready_cmd` antes de declarar
    el build fallido (ver `_ready_cmds.py`)."""

    interval_seconds: float
    timeout_seconds: float


@dataclass(frozen=True, slots=True)
class StartSpec:
    """Lo que horneado en `/etc/rayito/template.json` lee `rayd` (dominio
    compartido con `rayd_core::template::StartSpec`, mismo esquema JSON):
    el comando que arranca como proceso gestionado y, si lo hay, el que
    `rayd` sondea antes de responder 200 en `/ready`."""

    start_cmd: str
    ready_cmd: str | None = None
    user: str = DEFAULT_TEMPLATE_USER
    workdir: str | None = None
    envs: tuple[tuple[str, str], ...] = ()
    ready_poll: ReadyPoll | None = None


@dataclass(frozen=True, slots=True)
class TemplateSpec:
    """El estado inmutable que acumula el builder `Template` (`_dsl.py`):
    de qué imagen parte, en qué orden se compilan las instrucciones de
    cable, y el `StartSpec` opcional. `skip_cache` no activa ninguna caché
    de capas propia (0.6 no tiene una, investigación §3.4): sólo fuerza
    `Template.build(..., force=True)` a reconstruir en vez de reusar una
    versión existente con la misma configuración."""

    base: BaseImageRef | None = None
    steps: tuple[WireStep, ...] = field(default_factory=tuple)
    start: StartSpec | None = None
    skip_cache: bool = False

    def with_base(self, base: BaseImageRef) -> TemplateSpec:
        return TemplateSpec(
            base=base, steps=self.steps, start=self.start, skip_cache=self.skip_cache
        )

    def with_step(self, step: WireStep) -> TemplateSpec:
        return TemplateSpec(
            base=self.base, steps=(*self.steps, step), start=self.start, skip_cache=self.skip_cache
        )

    def with_start(self, start: StartSpec) -> TemplateSpec:
        return TemplateSpec(
            base=self.base, steps=self.steps, start=start, skip_cache=self.skip_cache
        )

    def with_skip_cache(self, *, skip_cache: bool) -> TemplateSpec:
        return TemplateSpec(
            base=self.base, steps=self.steps, start=self.start, skip_cache=skip_cache
        )
