"""Los tres efectos de la comprobación, como puertos pequeños: el estado entre
ejecuciones, el aviso y la métrica."""

from __future__ import annotations

from typing import Protocol

from quota_alert_check.decision import Level


class StateStore(Protocol):
    def last_level(self) -> Level: ...

    def save_level(self, level: Level) -> None: ...


class Notifier(Protocol):
    def publish(self, subject: str, message: str) -> None: ...


class MetricSink(Protocol):
    def put_fraction(self, region: str, fraction: float) -> None: ...
