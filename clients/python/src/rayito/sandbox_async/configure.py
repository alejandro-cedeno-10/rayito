"""Adaptador asíncrono de `ConfigureService` (M15 foundations): mismo
contrato que `sandbox_sync/configure.py` sobre un canal `grpc.aio`. Ver ese
módulo para el porqué de no tener consumidor todavía.
"""

from __future__ import annotations

from typing import Final, cast

import grpc

from rayito._transport import translate_rpc_error
from rayito.v1 import configure_pb2, configure_pb2_grpc

CONFIGURE_FEATURE: Final = "ConfigureSandbox"


async def call_configure(
    stub: configure_pb2_grpc.ConfigureServiceStub,
    request: configure_pb2.ConfigureRequest,
    *,
    timeout: float,
) -> configure_pb2.ConfigureResponse:
    try:
        response = await stub.Configure(request, timeout=timeout)
        return cast("configure_pb2.ConfigureResponse", response)
    except grpc.RpcError as exc:
        raise translate_rpc_error(exc, feature=CONFIGURE_FEATURE) from exc


async def call_configure_status(
    stub: configure_pb2_grpc.ConfigureServiceStub, *, timeout: float
) -> configure_pb2.ConfigureStatusResponse:
    try:
        response = await stub.ConfigureStatus(
            configure_pb2.ConfigureStatusRequest(), timeout=timeout
        )
        return cast("configure_pb2.ConfigureStatusResponse", response)
    except grpc.RpcError as exc:
        raise translate_rpc_error(exc, feature=CONFIGURE_FEATURE) from exc
