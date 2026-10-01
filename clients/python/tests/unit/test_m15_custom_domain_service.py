"""`rayito._custom_domain._service.CustomDomain` (m15-custom-domain, ADR-024)
sobre un `StackProvisioner` y un `KeyValueStoreWriter` falsos: construirlo no
llama a AWS, `deploy()` es una fachada fina de `OptionalStacks`,
`register`/`unregister`/`refresh` son las dos escrituras/borrados
encadenados por `ETag` que describe `_kvs.py`, y todo lo demás se valida
antes de tocar el KVS."""

from __future__ import annotations

from typing import Any

import boto3
import pytest

from rayito._custom_domain._domain import MAX_KVS_VALUE_BYTES
from rayito._custom_domain._service import CustomDomain, CustomDomainRoute
from rayito._custom_domain._service_async import AsyncCustomDomain
from rayito._stacks._model import StackStatus
from rayito.exceptions import CustomDomainException, InvalidArgumentException, StackException

from .fake_custom_domain import FakeKeyValueStoreWriter
from .fake_stacks import FakeStackProvisioner

PUBLIC_DOMAIN = "sbx.example.com"
KVS_ARN = "arn:aws:cloudfront::111122223333:key-value-store/abc123"


def _deployed_domain(
    *, clock: float = 1_000_000.0
) -> tuple[CustomDomain, FakeStackProvisioner, FakeKeyValueStoreWriter]:
    """Una `CustomDomain` que ya conoce su `kvs_arn` (pasado explícito, sin
    necesitar `deploy()`/`status()` en el fake): lista para
    `register`/`unregister`/`refresh`."""
    stacks = FakeStackProvisioner()
    kvs = FakeKeyValueStoreWriter()
    domain = CustomDomain(
        public_domain=PUBLIC_DOMAIN,
        kvs_arn=KVS_ARN,
        provisioner=stacks,
        kvs_writer=kvs,
        clock=lambda: clock,
    )
    kvs.seed(KVS_ARN)
    return domain, stacks, kvs


def test_constructing_custom_domain_makes_no_call() -> None:
    stacks = FakeStackProvisioner()
    kvs = FakeKeyValueStoreWriter()
    CustomDomain(public_domain=PUBLIC_DOMAIN, provisioner=stacks, kvs_writer=kvs)
    assert stacks.calls == []
    assert kvs.calls == []


def test_constructing_the_real_adapters_builds_no_boto3_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Sin `provisioner=`/`kvs_writer=` (los adaptadores reales,
    `CloudFormationProvisioner`/`CloudFrontKvsWriter`), construir
    `CustomDomain` sigue sin llamar a `boto3.session.Session.client`: ambos
    sólo construyen `LazyClient`s (§9.2 de la arquitectura de M15)."""
    calls: list[str] = []
    original = boto3.session.Session.client

    def recording_client(self: Any, service_name: str, *args: Any, **kwargs: Any) -> Any:
        calls.append(service_name)
        return original(self, service_name, *args, **kwargs)

    monkeypatch.setattr(boto3.session.Session, "client", recording_client)
    CustomDomain(public_domain=PUBLIC_DOMAIN, region="us-east-1")
    assert calls == []


def test_an_invalid_public_domain_is_rejected_at_construction() -> None:
    with pytest.raises(InvalidArgumentException):
        CustomDomain(public_domain="-not-valid-")


def test_deploy_delegates_to_optional_stacks_with_the_right_parameters() -> None:
    stacks = FakeStackProvisioner()
    kvs = FakeKeyValueStoreWriter()
    domain = CustomDomain(public_domain=PUBLIC_DOMAIN, provisioner=stacks, kvs_writer=kvs)
    status = domain.deploy(certificate_arn="arn:aws:acm:us-east-1:111122223333:certificate/abc")
    assert status.state == "CREATE_COMPLETE"
    assert [call[0] for call in stacks.calls] == ["describe", "create", "wait", "describe"]


def test_deploy_caches_the_kvs_arn_from_the_stack_outputs() -> None:
    stacks = FakeStackProvisioner()
    stacks.stacks["rayito-custom-domain"] = StackStatus(
        name="rayito-custom-domain", state="CREATE_COMPLETE", outputs={"KvsArn": KVS_ARN}
    )
    domain = CustomDomain(public_domain=PUBLIC_DOMAIN, provisioner=stacks)
    # Una pila ya desplegada con ese output: deploy() la actualiza (no la
    # crea) y sigue cacheando KvsArn igual que lo haría status().
    domain.deploy(certificate_arn="arn:aws:acm:us-east-1:111122223333:certificate/abc")
    assert domain.kvs_arn() == KVS_ARN


def test_status_also_caches_the_kvs_arn() -> None:
    stacks = FakeStackProvisioner()
    stacks.stacks["rayito-custom-domain"] = StackStatus(
        name="rayito-custom-domain", state="CREATE_COMPLETE", outputs={"KvsArn": KVS_ARN}
    )
    domain = CustomDomain(public_domain=PUBLIC_DOMAIN, provisioner=stacks)
    domain.status()
    assert domain.kvs_arn() == KVS_ARN


def test_kvs_arn_without_a_deploy_or_status_call_raises() -> None:
    domain = CustomDomain(public_domain=PUBLIC_DOMAIN, provisioner=FakeStackProvisioner())
    with pytest.raises(CustomDomainException, match="KvsArn"):
        domain.kvs_arn()


def test_an_explicit_kvs_arn_needs_no_status_call() -> None:
    kvs = FakeKeyValueStoreWriter()
    kvs.seed(KVS_ARN)
    domain = CustomDomain(
        public_domain=PUBLIC_DOMAIN,
        kvs_arn=KVS_ARN,
        provisioner=FakeStackProvisioner(),
        kvs_writer=kvs,
    )
    assert domain.kvs_arn() == KVS_ARN


def test_host_for_is_pure_and_needs_no_deploy() -> None:
    domain = CustomDomain(public_domain=PUBLIC_DOMAIN, provisioner=FakeStackProvisioner())
    assert domain.host_for("ws-7", 8000) == "8000-ws-7.sbx.example.com"


def test_register_writes_both_keys_with_a_chained_etag() -> None:
    domain, _stacks, kvs = _deployed_domain(clock=1_000_000.0)
    route = domain.register(
        "ws-7", 8000, endpoint="10.0.0.1.lambda-url.us-east-1.on.aws", jwe="a-jwe", ttl_seconds=2400
    )
    assert route.host == "8000-ws-7.sbx.example.com"
    assert route.expires_at.timestamp() == 1_000_000.0 + 2400
    assert kvs.stores[KVS_ARN]["j:8000-ws-7"] == "a-jwe"
    stored_meta = kvs.stores[KVS_ARN]["m:8000-ws-7"]
    assert '"e":"10.0.0.1.lambda-url.us-east-1.on.aws"' in stored_meta
    assert [call[0] for call in kvs.calls] == ["describe", "put", "put"]


def test_register_with_a_traffic_token_stores_only_its_digest() -> None:
    domain, _stacks, kvs = _deployed_domain()
    domain.register("ws-7", 8000, endpoint="e", jwe="a-jwe", traffic_token="secret", ttl_seconds=60)
    stored_meta = kvs.stores[KVS_ARN]["m:8000-ws-7"]
    assert "secret" not in stored_meta


def test_register_rejects_a_non_positive_ttl_before_touching_the_kvs() -> None:
    domain, _stacks, kvs = _deployed_domain()
    with pytest.raises(InvalidArgumentException):
        domain.register("ws-7", 8000, endpoint="e", jwe="a-jwe", ttl_seconds=0)
    assert kvs.calls == []


def test_register_rejects_an_oversized_jwe_before_touching_the_kvs() -> None:
    domain, _stacks, kvs = _deployed_domain()
    with pytest.raises(CustomDomainException):
        domain.register(
            "ws-7", 8000, endpoint="e", jwe="x" * (MAX_KVS_VALUE_BYTES + 1), ttl_seconds=60
        )
    assert kvs.calls == []


def test_refresh_rewrites_only_the_jwe_key() -> None:
    domain, _stacks, kvs = _deployed_domain(clock=1_000_000.0)
    route = domain.register("ws-7", 8000, endpoint="e", jwe="old-jwe", ttl_seconds=60)
    meta_before = kvs.stores[KVS_ARN]["m:8000-ws-7"]
    refreshed = domain.refresh(route, jwe="new-jwe", ttl_seconds=2400)
    assert kvs.stores[KVS_ARN]["j:8000-ws-7"] == "new-jwe"
    assert kvs.stores[KVS_ARN]["m:8000-ws-7"] == meta_before
    assert refreshed.expires_at.timestamp() == 1_000_000.0 + 2400


def test_unregister_removes_both_keys() -> None:
    domain, _stacks, kvs = _deployed_domain()
    domain.register("ws-7", 8000, endpoint="e", jwe="a-jwe", ttl_seconds=60)
    domain.unregister("ws-7", 8000)
    assert "j:8000-ws-7" not in kvs.stores[KVS_ARN]
    assert "m:8000-ws-7" not in kvs.stores[KVS_ARN]


def test_unregister_a_route_that_never_existed_is_a_no_op() -> None:
    domain, _stacks, _kvs = _deployed_domain()
    domain.unregister("never-registered", 8000)  # no debe lanzar


def test_destroy_forgets_a_kvs_arn_learned_from_status() -> None:
    stacks = FakeStackProvisioner()
    stacks.stacks["rayito-custom-domain"] = StackStatus(
        name="rayito-custom-domain", state="CREATE_COMPLETE", outputs={"KvsArn": KVS_ARN}
    )
    domain = CustomDomain(public_domain=PUBLIC_DOMAIN, provisioner=stacks)
    domain.status()
    assert domain.kvs_arn() == KVS_ARN
    domain.destroy()
    assert stacks.calls[-1][0] == "wait"
    with pytest.raises(CustomDomainException):
        domain.kvs_arn()


async def test_async_custom_domain_mirrors_the_sync_one() -> None:
    stacks = FakeStackProvisioner()
    kvs = FakeKeyValueStoreWriter()
    domain = AsyncCustomDomain(
        public_domain=PUBLIC_DOMAIN,
        kvs_arn=KVS_ARN,
        provisioner=stacks,
        kvs_writer=kvs,
        clock=lambda: 0.0,
    )
    kvs.seed(KVS_ARN)
    certificate_arn = "arn:aws:acm:us-east-1:111122223333:certificate/abc"
    status = await domain.deploy(certificate_arn=certificate_arn)
    assert status.state == "CREATE_COMPLETE"
    route = await domain.register("ws-7", 8000, endpoint="e", jwe="a-jwe", ttl_seconds=60)
    assert isinstance(route, CustomDomainRoute)
    await domain.unregister("ws-7", 8000)
    await domain.destroy()
    assert stacks.calls[-1][0] == "wait"


def test_blocked_deploy_surfaces_stack_exception() -> None:
    stacks = FakeStackProvisioner()
    from rayito._stacks._model import StackStatus

    stacks.stacks["rayito-custom-domain"] = StackStatus(
        name="rayito-custom-domain", state="ROLLBACK_COMPLETE"
    )
    domain = CustomDomain(public_domain=PUBLIC_DOMAIN, provisioner=stacks)
    with pytest.raises(StackException):
        domain.deploy(certificate_arn="arn:aws:acm:us-east-1:111122223333:certificate/abc")
