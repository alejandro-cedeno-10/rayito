"""El aspecto de la pila: el tema (y, opcional, el logo) que el administrador
guarda en parámetros de SSM, leídos una vez por entorno de ejecución. Pasan
por los mismos `parse_theme` y `parse_logo` que `rayito dashboard --theme`,
así que un tema no puede inyectar CSS ni scripts. Un parámetro ausente, ilegible
o inválido no tumba el panel: se sirve el aspecto de fábrica y sólo se registra
el motivo (nunca el contenido).
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable, Mapping
from typing import Any, Final

from botocore.exceptions import BotoCoreError, ClientError

from rayito._dashboard._api._static import appearance_files, load_static_files
from rayito._dashboard._theme import Appearance, Branding, Logo, Theme, parse_logo, parse_theme
from rayito._limits import DASHBOARD_STACK_THEME_REFRESH_SECONDS
from rayito.exceptions import InvalidArgumentException

logger = logging.getLogger("rayito.dashboard")

#: El formato de un tema en SSM: JSON (el de `examples/dashboard-theme/`).
THEME_FORMAT: Final = "json"


class ParameterUnavailable(Exception):
    """No se pudo leer un parámetro de SSM (no existe, sin permiso, sin red)."""


def read_parameter(ssm: Any, name: str) -> str:
    try:
        return str(ssm.get_parameter(Name=name)["Parameter"]["Value"])
    except (ClientError, BotoCoreError, KeyError):
        raise ParameterUnavailable from None


def _theme(ssm: Any, name: str | None) -> Theme | None:
    if name is None:
        return None
    text = read_parameter(ssm, name)
    try:
        return parse_theme(text, fmt=THEME_FORMAT)
    except InvalidArgumentException as error:
        logger.warning("dashboard theme: tema descartado (%s)", error)
        return None


def _logo(ssm: Any, name: str | None) -> Logo | None:
    if name is None:
        return None
    text = read_parameter(ssm, name)
    try:
        return parse_logo(text.encode())
    except InvalidArgumentException as error:
        logger.warning("dashboard theme: logo descartado (%s)", error)
        return None


def load_appearance(
    ssm: Any, theme_parameter: str | None, logo_parameter: str | None
) -> Appearance:
    return Appearance(theme=_theme(ssm, theme_parameter), logo=_logo(ssm, logo_parameter))


class LiveAppearance:
    """El aspecto vigente. Cambiar el parámetro de SSM no reinicia la función
    (el cambio no toca su configuración), así que se vuelve a leer como mucho
    cada `DASHBOARD_STACK_THEME_REFRESH_SECONDS`, en la primera petición que lo
    encuentra vencido. Si SSM no responde, se conserva el aspecto anterior."""

    def __init__(
        self,
        load: Callable[[], Appearance],
        *,
        clock: Callable[[], float] = time.monotonic,
        refresh_seconds: float = DASHBOARD_STACK_THEME_REFRESH_SECONDS,
    ) -> None:
        self._load = load
        self._clock = clock
        self._refresh = refresh_seconds
        self._shell = load_static_files()
        self._lock = threading.Lock()
        self._loaded_at = clock()
        self._appearance = Appearance()
        self._reload()
        self._files = {**self._shell, **appearance_files(self._appearance)}

    def _refreshed(self) -> None:
        with self._lock:
            now = self._clock()
            if now - self._loaded_at < self._refresh:
                return
            self._loaded_at = now
            self._reload()

    def _reload(self) -> None:
        try:
            self._appearance = self._load()
        except ParameterUnavailable:
            logger.warning("dashboard theme: no se pudo leer un parámetro de SSM")
        self._files = {**self._shell, **appearance_files(self._appearance)}

    @property
    def branding(self) -> Branding:
        self._refreshed()
        return self._appearance.branding

    @property
    def files(self) -> Mapping[str, tuple[bytes, str]]:
        self._refreshed()
        return self._files
