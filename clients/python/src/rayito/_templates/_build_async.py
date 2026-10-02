"""`AsyncTemplate` sobre `_build.py`: cada llamada de plano de control
(`boto3`, bloqueante) va por `asyncio.to_thread`, igual que el resto del
SDK async (`sandbox_async/*`, `rayito._secrets.SecretCache`). Nada nuevo
aquí salvo el `await`: la lógica vive en `_build.py`."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from pathlib import Path
from typing import Any

from rayito._images import DEFAULT_BUILD_TIMEOUT_SECONDS, DEFAULT_MEMORY_MIB
from rayito._templates import _build
from rayito._templates._models import BuildHandle, BuildInfo, BuildStatus


async def build(
    template: Any,
    name: str,
    *,
    bucket: str,
    memory_mb: int = DEFAULT_MEMORY_MIB,
    cpu_count: int | None = None,
    force: bool = False,
    timeout: float = DEFAULT_BUILD_TIMEOUT_SECONDS,
    base_image_version: str | None = None,
    build_role_arn: str | None = None,
    on_build_logs: Callable[[str], None] | None = None,
    region: str | None = None,
    session: Any = None,
    context_dir: Path | None = None,
) -> BuildInfo:
    return await asyncio.to_thread(
        _build.build,
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


async def build_in_background(
    template: Any,
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
    return await asyncio.to_thread(
        _build.build_in_background,
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


async def get_build_status(handle: BuildHandle) -> BuildStatus:
    return await asyncio.to_thread(_build.get_build_status, handle)


async def template_exists(name: str, *, region: str | None = None, session: Any = None) -> bool:
    return await asyncio.to_thread(_build.template_exists, name, region=region, session=session)
