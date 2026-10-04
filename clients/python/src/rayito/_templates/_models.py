"""Los tres valores que cruzan la frontera de `_build.py`/`_build_async.py`
hacia `Template`/`AsyncTemplate`: puros, sin ningún cliente `boto3` dentro
(`BuildHandle` guarda sólo `region`/`session`, los mismos datos que
`Template.build_in_background()` recibió, para que `get_build_status()`
pueda reconstruir el mismo cliente perezoso más tarde)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

#: `IN_PROGRESS` mientras `version_state` de `GetMicrovmImageVersion` no se
#: haya asentado; `SUCCESSFUL`/`FAILED` son sus dos estados finales
#: (`cli/_publish.py` `SETTLED_VERSION_STATES`, repetido aquí porque ese
#: módulo pertenece a sizes-catalog).
BuildState = Literal["IN_PROGRESS", "SUCCESSFUL", "FAILED"]


@dataclass(frozen=True, slots=True)
class BuildInfo:
    """Lo que devuelve un build que terminó bien. `template_id` es el ARN
    de la imagen (pásalo a `Sandbox.create(template_id)`); `build_id` es
    `"<imageVersion>/<buildId>"`, para `get_build_status`/`template logs`."""

    template_id: str
    build_id: str
    alias: str


@dataclass(frozen=True, slots=True)
class BuildHandle:
    """Lo que devuelve `build_in_background()`: deja la petición ya
    aceptada (`create-microvm-image`/`update-microvm-image` respondió) y
    guarda lo necesario para que `get_build_status()` vuelva a preguntar
    sin repetir el build."""

    arn: str
    version: str
    name: str
    region: str | None = None
    session: Any = None


@dataclass(frozen=True, slots=True)
class BuildStatus:
    """El estado de un build en curso o terminado. `info`/`error` son
    mutuamente excluyentes y ambos `None` mientras `state ==
    "IN_PROGRESS"`."""

    state: BuildState
    info: BuildInfo | None = None
    error_message: str | None = None
