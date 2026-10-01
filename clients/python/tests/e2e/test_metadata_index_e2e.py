"""M14 contra AWS real (`RAYITO_E2E=1` y `RAYITO_E2E_INDEX_TABLE` con la tabla
desplegada desde `infra/metadata-index.yaml`): tres sandboxes con
`index=DynamoDbIndex(...)`, dos en pausa; `Sandbox.list(metadata=,
states=["SUSPENDED"], index=)` devuelve exactamente esos dos y, después,
`get-microvm` muestra que siguen `SUSPENDED` (el listado no los despertó).
Luego se reanuda uno: su `startedAt` no cambia (±1 s, IDX-1 c) y el listado
con índice lo sigue encontrando, ya `RUNNING`.

Coste: tres sandboxes durante < 5 min (~$0,05) + 3 `PutItem` + 1
`BatchGetItem` (< $0,00001)."""

from __future__ import annotations

import contextlib
import os
import secrets as stdlib_secrets

import pytest

from rayito import DynamoDbIndex, IdlePolicy, Sandbox
from rayito._aws import LambdaMicrovmsControlPlane
from rayito.exceptions import SandboxNotFoundException

from .conftest import TEST_SANDBOX_TIMEOUT_SECONDS, E2ESettings

pytestmark = pytest.mark.e2e

INDEX_TABLE_VAR = "RAYITO_E2E_INDEX_TABLE"


def test_list_by_metadata_over_suspended_sandboxes_without_waking_them(
    e2e_settings: E2ESettings,
    control_plane: LambdaMicrovmsControlPlane,
    template_arn: str,
) -> None:
    table = os.environ.get(INDEX_TABLE_VAR)
    if not table:
        pytest.fail(f"este e2e necesita {INDEX_TABLE_VAR} (despliega infra/metadata-index.yaml)")
    index = DynamoDbIndex(table, region=e2e_settings.region or control_plane.region)
    run = stdlib_secrets.token_hex(6)
    sandboxes: list[Sandbox] = []
    try:
        for _ in range(3):
            sandboxes.append(
                Sandbox.create(
                    template_arn,
                    timeout=TEST_SANDBOX_TIMEOUT_SECONDS,
                    idle=IdlePolicy(max_idle_seconds=600, auto_resume=True),
                    metadata={"suite": "m14", "run": run},
                    index=index,
                    control_plane=control_plane,
                )
            )
        paused = sandboxes[:2]
        started = {s.sandbox_id: control_plane.get_microvm(s.sandbox_id).started_at for s in paused}
        for sandbox in paused:
            assert sandbox.pause() is True

        found = list(
            Sandbox.list(
                metadata={"run": run},
                states=["SUSPENDED"],
                index=index,
                control_plane=control_plane,
            )
        )

        assert sorted(item.sandbox_id for item in found) == sorted(s.sandbox_id for s in paused)
        assert all(item.metadata == {"suite": "m14", "run": run} for item in found)
        for sandbox in paused:
            assert control_plane.get_microvm(sandbox.sandbox_id).state == "SUSPENDED"

        resumed = paused[0]
        resumed.resume()
        after = control_plane.get_microvm(resumed.sandbox_id).started_at
        assert abs((after - started[resumed.sandbox_id]).total_seconds()) <= 1
        running = list(
            Sandbox.list(
                metadata={"run": run}, states=["RUNNING"], index=index, control_plane=control_plane
            )
        )
        assert resumed.sandbox_id in {item.sandbox_id for item in running}
    finally:
        for sandbox in sandboxes:
            with contextlib.suppress(SandboxNotFoundException):
                Sandbox.kill(sandbox.sandbox_id, control_plane=control_plane)
            sandbox.close()
