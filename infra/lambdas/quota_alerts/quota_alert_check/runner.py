"""Un ciclo de la comprobación: medir, publicar la métrica, decidir y, sólo si
el nivel sube, avisar. El estado se guarda *después* de avisar: si SNS falla,
la ejecución falla (EventBridge reintenta) y el siguiente ciclo vuelve a
intentar el aviso en vez de darlo por enviado. El estado guarda el nivel más
alto alcanzado hasta que el uso vuelve a `ok` (histéresis)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from quota_alert_check.config import AlertConfig
from quota_alert_check.decision import (
    Level,
    body,
    classify,
    level_to_remember,
    should_notify,
    subject,
)
from quota_alert_check.ports import MetricSink, Notifier, StateStore

from rayito._dashboard._domain import QuotaReport


class QuotaSource(Protocol):
    def report(self) -> QuotaReport: ...


@dataclass(frozen=True)
class CheckResult:
    level: Level
    fraction: float | None
    notified: bool


class QuotaAlertCheck:
    def __init__(
        self,
        *,
        config: AlertConfig,
        quotas: QuotaSource,
        state: StateStore,
        notifier: Notifier,
        metrics: MetricSink,
    ) -> None:
        self._config = config
        self._quotas = quotas
        self._state = state
        self._notifier = notifier
        self._metrics = metrics

    def run(self) -> CheckResult:
        memory = self._quotas.report().memory
        if memory.fraction is None or memory.quota_gb is None:
            return CheckResult("ok", None, notified=False)
        self._metrics.put_fraction(self._config.region, memory.fraction)
        level = classify(
            memory.fraction,
            self._config.warning_fraction,
            self._config.critical_fraction,
        )
        previous = self._state.last_level()
        notified = should_notify(previous, level)
        if notified:
            self._notifier.publish(
                subject(level, self._config.region, memory.fraction),
                body(
                    level,
                    self._config.region,
                    memory.allocated_gb,
                    memory.quota_gb,
                    memory.sandboxes,
                    memory.unresolved,
                ),
            )
        self._state.save_level(level_to_remember(previous, level))
        return CheckResult(level, memory.fraction, notified)
