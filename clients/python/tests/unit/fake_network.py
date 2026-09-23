"""`NetworkService` falso en proceso con el contrato de `rayd` en M9 (ADR-012).

Guarda cada `UpdateNetworkRequest`, exige `x-access-token` en los dos RPCs
y responde el `NetworkState` que `rayd` devolvería: las listas tal como
llegaron, `egress_proxy_configured` sin eco de la dirección ni de las
credenciales, y `enforcement` = `NONE` para una política que no restringe
o el valor programado en `enforcement` para una que sí. `fail_with`
programa un status para el siguiente `UpdateNetwork` (una sola vez).
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field

import grpc

from rayito.v1 import network_pb2

from .fake_process import require_access_token

DEFAULT_LOCAL_PROXY_PORT = 41_234


def restricts(policy: network_pb2.NetworkPolicy) -> bool:
    return bool(policy.deny_out) or policy.HasField("egress_proxy")


@dataclass
class FakeNetworkService:
    token_sha256: str
    enforcement: network_pb2.EgressEnforcement = network_pb2.EGRESS_ENFORCEMENT_GUEST_ROUTES
    fail_with: tuple[grpc.StatusCode, str] | None = None
    update_requests: list[network_pb2.UpdateNetworkRequest] = field(default_factory=list)
    get_calls: int = 0
    state: network_pb2.NetworkState = field(
        default_factory=lambda: network_pb2.NetworkState(
            enforcement=network_pb2.EGRESS_ENFORCEMENT_NONE
        )
    )
    lock: threading.Lock = field(default_factory=threading.Lock)

    def UpdateNetwork(
        self, request: network_pb2.UpdateNetworkRequest, context: grpc.ServicerContext
    ) -> network_pb2.NetworkState:
        require_access_token(context, self.token_sha256)
        with self.lock:
            self.update_requests.append(request)
            failure, self.fail_with = self.fail_with, None
        if failure is not None:
            context.abort(*failure)
        state = self.state_for(request.policy)
        with self.lock:
            self.state = state
        return state

    def GetNetwork(
        self, request: network_pb2.GetNetworkRequest, context: grpc.ServicerContext
    ) -> network_pb2.NetworkState:
        require_access_token(context, self.token_sha256)
        with self.lock:
            self.get_calls += 1
            return self.state

    def state_for(self, policy: network_pb2.NetworkPolicy) -> network_pb2.NetworkState:
        enforcing = restricts(policy)
        proxy_mode = self.enforcement == network_pb2.EGRESS_ENFORCEMENT_GUEST_ROUTES_AND_PROXY
        return network_pb2.NetworkState(
            allow_out=list(policy.allow_out),
            deny_out=list(policy.deny_out),
            egress_proxy_configured=policy.HasField("egress_proxy"),
            enforcement=self.enforcement if enforcing else network_pb2.EGRESS_ENFORCEMENT_NONE,
            local_proxy_port=DEFAULT_LOCAL_PROXY_PORT if enforcing and proxy_mode else 0,
        )

    @property
    def last_policy(self) -> network_pb2.NetworkPolicy:
        with self.lock:
            return self.update_requests[-1].policy
