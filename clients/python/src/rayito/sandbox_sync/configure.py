"""Adaptador síncrono de `ConfigureService` (M15 foundations): construye la
llamada sobre el stub generado y traduce los errores de gRPC con la misma
tabla que el resto del SDK (`rayito._transport.translate_rpc_error`). El
token de acceso ya lo añade el `ProxyAuthPlugin` del canal, como en
cualquier otro RPC autenticado.

Sin consumidor todavía: ninguna función 0.6 rellena una sección de
`ConfigureRequest` (`_feature_options.plan_features` lanza antes de que
`create()` llegue a necesitar esto). Existe para que la primera función que
sí la rellene no tenga que escribir el adaptador.
"""

from __future__ import annotations

from typing import Final, cast

import grpc

from rayito._transport import translate_rpc_error
from rayito.v1 import configure_pb2, configure_pb2_grpc

CONFIGURE_FEATURE: Final = "ConfigureSandbox"


def call_configure(
    stub: configure_pb2_grpc.ConfigureServiceStub,
    request: configure_pb2.ConfigureRequest,
    *,
    timeout: float,
) -> configure_pb2.ConfigureResponse:
    try:
        return cast("configure_pb2.ConfigureResponse", stub.Configure(request, timeout=timeout))
    except grpc.RpcError as exc:
        raise translate_rpc_error(exc, feature=CONFIGURE_FEATURE) from exc


def call_configure_status(
    stub: configure_pb2_grpc.ConfigureServiceStub, *, timeout: float
) -> configure_pb2.ConfigureStatusResponse:
    try:
        response = stub.ConfigureStatus(configure_pb2.ConfigureStatusRequest(), timeout=timeout)
        return cast("configure_pb2.ConfigureStatusResponse", response)
    except grpc.RpcError as exc:
        raise translate_rpc_error(exc, feature=CONFIGURE_FEATURE) from exc
