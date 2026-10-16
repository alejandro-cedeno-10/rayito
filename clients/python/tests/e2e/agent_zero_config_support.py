"""Lo común a `test_agent_zero_config.py`: los quince agentes de ejemplo
(`testdata/agent-samples/`) preparados para desplegarlos sin tocar su código,
con un nombre propio de la ejecución, y el libro de gasto que corta la
ejecución antes de pasar del tope.

Variables de entorno, además de `RAYITO_E2E=1` y `RAYITO_TEMPLATE` (la imagen
base desechable de la ejecución):

- `RAYITO_E2E_RUN_ID`: id de la ejecución; va en el nombre de cada imagen y de
  cada despliegue, y la limpieza borra sólo lo que lo lleva.
- `RAYITO_E2E_ZC_BUCKET`: bucket del artefacto de las imágenes.
- `RAYITO_E2E_ZC_BASE`: imagen base con capabilities (`rayito-base-caps`
  desechable) de la que parten las imágenes de los agentes.
- `RAYITO_E2E_BEDROCK_SECRET`: nombre del secreto que guarda `Bearer <clave de
  API de Bedrock de corta duración>`.
- `RAYITO_E2E_ZC_SAMPLES` (opcional): ids del manifiesto separados por comas.
- `RAYITO_E2E_ZC_LEDGER` (opcional): fichero donde se anotan las imágenes y los
  segundos de VM; con él, la ejecución se para sola si la proyección pasa de
  `COST_STOP_USD`.
- `RAYITO_E2E_BEDROCK_REGION` (opcional).

Cada ejemplo se despliega dos veces: sin opciones (`deploy_agent(ruta)` con
sólo el nombre y la base en un `rayito-agent.toml` mínimo) y con opciones
propias (4096 MiB, 2 sesiones por VM, cola de 2, de 1 a 2 VMs con 1 caliente).
"""

from __future__ import annotations

import json
import os
import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

import pytest

REPO_ROOT: Final = Path(__file__).resolve().parents[4]
SAMPLES_DIR: Final = REPO_ROOT / "testdata" / "agent-samples"
MANIFEST: Final[dict[str, Any]] = json.loads((SAMPLES_DIR / "manifest.json").read_text("utf-8"))
RUN_ID_VAR: Final = "RAYITO_E2E_RUN_ID"
BUCKET_VAR: Final = "RAYITO_E2E_ZC_BUCKET"
BASE_VAR: Final = "RAYITO_E2E_ZC_BASE"
SECRET_VAR: Final = "RAYITO_E2E_BEDROCK_SECRET"
SAMPLES_VAR: Final = "RAYITO_E2E_ZC_SAMPLES"
LEDGER_VAR: Final = "RAYITO_E2E_ZC_LEDGER"
REGION_VAR: Final = "RAYITO_E2E_BEDROCK_REGION"
DEFAULT_REGION: Final = "us-east-1"
GATEWAY_NAME: Final = "bedrock"
IGNORED_DIRS: Final = (".venv", "node_modules", "__pycache__", ".git")
CUSTOM = MANIFEST["custom"]
IDLE_SECONDS: Final = 60
QUEUE_MAX: Final = 2
QUEUE_TIMEOUT_SECONDS: Final = 60
#: Precios de lista (`AWS_API_NOTES.md`, coste): una VM de 2 GB, lo que cuesta
#: por segundo, y una versión de imagen (escritura del snapshot y su semana
#: mínima de almacenamiento).
VM_USD_PER_SECOND_2GB: Final = 0.126 / 3600
IMAGE_BUILD_USD: Final = 0.07
MODEL_USD_PER_RUN: Final = 0.01
COST_TARGET_USD: Final = 6.0
COST_STOP_USD: Final = 8.0
MEMORY_BASE_MIB: Final = 2048
PARALLEL_CALLS: Final = 5
SUSPEND_WAIT_SECONDS: Final = 150
RECOVERY_TIMEOUT_SECONDS: Final = 90
POLL_SECONDS: Final = 3.0


def selected_samples() -> list[dict[str, Any]]:
    wanted = {name for name in os.environ.get(SAMPLES_VAR, "").split(",") if name}
    return [s for s in MANIFEST["samples"] if not wanted or s["id"] in wanted]


def sample_ids() -> list[str]:
    return [sample["id"] for sample in selected_samples()]


def region() -> str:
    return os.environ.get(REGION_VAR) or os.environ.get("AWS_REGION") or DEFAULT_REGION


def require(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        pytest.skip(f"exporta {name} (cabecera de agent_zero_config_support.py)")
    return value


def project_name(sample: dict[str, Any], variant: str) -> str:
    """`zc-<ejecución>-<n><variante>`: corto, único por ejecución y válido como
    nombre de imagen. La variante es `""` (sin opciones), `"c"` (opciones
    propias) o `"d"` (sesiones dedicadas)."""
    index = [s["id"] for s in MANIFEST["samples"]].index(sample["id"])
    return f"zc-{require(RUN_ID_VAR)}-{index:02d}{variant}"


def project_file(name: str, base: str, variant: str) -> str:
    """El `rayito-agent.toml` mínimo: el nombre y la base (la ejecución los
    necesita propios) y, con una variante, las opciones. Todo lo demás lo decide
    la detección. `c` usa la memoria de las opciones propias y `d` la de la
    imagen por defecto con sesiones dedicadas."""
    lines = [
        'schema = "rayito.agent-project/1"',
        f'name = "{name}"',
        "",
        "[image]",
        f'base = "{base}"',
    ]
    if variant == "c":
        lines.append(f"memory_mib = {CUSTOM['memoryMib']}")
    if variant:
        sessions = "dedicated" if variant == "d" else "shared"
        lines += [
            "",
            "[serve]",
            f"max_concurrency = {CUSTOM['maxSessions']}",
            f"queue_max = {QUEUE_MAX}",
            f"queue_timeout_seconds = {QUEUE_TIMEOUT_SECONDS}",
            f"idle_seconds = {IDLE_SECONDS}",
            "",
            "[scaling]",
            f"min_vms = {CUSTOM['minVms']}",
            f"max_vms = {CUSTOM['maxVms']}",
            f"warm_vms = {CUSTOM['warmVms']}",
            f'sessions = "{sessions}"',
        ]
    return "\n".join(lines) + "\n"


def staged_sample(sample: dict[str, Any], tmp_path: Path, *, variant: str = "") -> Path:
    """Una copia del ejemplo (sin entornos instalados) con su fichero mínimo."""
    name = project_name(sample, variant)
    target = tmp_path / name
    shutil.copytree(
        SAMPLES_DIR / sample["dir"], target, ignore=shutil.ignore_patterns(*IGNORED_DIRS)
    )
    (target / "rayito-agent.toml").write_text(
        project_file(name, require(BASE_VAR), variant), encoding="utf-8"
    )
    return target


@dataclass
class Ledger:
    """El gasto de la ejecución: versiones de imagen construidas y segundos de
    VM (con su memoria). Se anota en un fichero JSON de líneas para que varias
    ejecuciones en paralelo vean el mismo total."""

    path: Path | None

    @classmethod
    def from_environment(cls) -> Ledger:
        raw = os.environ.get(LEDGER_VAR)
        return cls(Path(raw) if raw else None)

    def add(self, kind: str, **fields: Any) -> None:
        if self.path is None:
            return
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({"t": time.time(), "kind": kind, **fields}) + "\n")

    def projected_usd(self) -> float:
        if self.path is None or not self.path.exists():
            return 0.0
        total = 0.0
        for line in self.path.read_text(encoding="utf-8").splitlines():
            entry = json.loads(line)
            if entry["kind"] == "build":
                total += IMAGE_BUILD_USD
            elif entry["kind"] == "vm":
                total += (
                    entry["seconds"] * VM_USD_PER_SECOND_2GB * entry["memory_mib"] / MEMORY_BASE_MIB
                )
            elif entry["kind"] == "model":
                total += MODEL_USD_PER_RUN * entry.get("calls", 1)
        return total

    def guard(self) -> None:
        spent = self.projected_usd()
        if spent > COST_STOP_USD:
            pytest.exit(
                f"el gasto proyectado ({spent:.2f} USD) pasa del tope de parada "
                f"({COST_STOP_USD} USD)",
                returncode=3,
            )
