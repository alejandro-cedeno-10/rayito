"""Dobles de la receta de la flota (ADR-034): una tienda en memoria con las
llamadas que recibió y los errores que se le piden. Nunca toca AWS."""

from __future__ import annotations

from dataclasses import dataclass, field

from rayito._agent._fleet_recipe import RecipeWarning


@dataclass
class MemoryRecipeStore:
    """`FleetRecipeStore` en memoria: `calls` anota cada operación
    (`save`/`load`/`delete`, con la ruta) y `failures` hace fallar una
    operación con la excepción dada."""

    values: dict[str, str] = field(default_factory=dict)
    calls: list[tuple[str, str]] = field(default_factory=list)
    failures: dict[str, Exception] = field(default_factory=dict)

    def save(self, parameter: str, value: str) -> None:
        self._record("save", parameter)
        self.values[parameter] = value

    def load(self, parameter: str) -> str | None:
        self._record("load", parameter)
        return self.values.get(parameter)

    def delete(self, parameter: str) -> None:
        self._record("delete", parameter)
        self.values.pop(parameter, None)

    def _record(self, operation: str, parameter: str) -> None:
        self.calls.append((operation, parameter))
        failure = self.failures.get(operation)
        if failure is not None:
            raise failure

    @property
    def operations(self) -> list[str]:
        return [operation for operation, _ in self.calls]


def denied() -> RecipeWarning:
    return RecipeWarning("recipe_access_denied")
