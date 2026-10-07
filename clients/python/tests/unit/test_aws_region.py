"""Resolución de la región: `region=` > sesión > `AWS_REGION` > cadena de boto3."""

from __future__ import annotations

import boto3
import pytest

from rayito._aws import LambdaMicrovmsControlPlane, LazyClient
from rayito._aws_region import REGION_ENV_VAR, aws_session, resolve_region
from rayito._lifecycle_events._aws import BotoEventsGateway
from rayito._secrets import SecretStore
from rayito._templates._build import _Clients
from rayito._volumes._store import VolumeStore
from rayito.exceptions import InvalidArgumentException

EXPLICIT = "eu-west-1"
FROM_ENV = "us-west-2"
FROM_DEFAULT_ENV = "ap-south-1"
FROM_SESSION = "eu-central-1"
FILE_SYSTEM_ID = "fs-0123456789abcdef0"


@pytest.fixture(autouse=True)
def _region_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(REGION_ENV_VAR, FROM_ENV)
    monkeypatch.setenv("AWS_DEFAULT_REGION", FROM_DEFAULT_ENV)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.delenv("AWS_PROFILE", raising=False)


def test_explicit_region_wins() -> None:
    assert resolve_region(EXPLICIT) == EXPLICIT


def test_aws_region_beats_aws_default_region() -> None:
    assert resolve_region(None) == FROM_ENV
    assert aws_session(None, None).region_name == FROM_ENV


def test_caller_session_region_beats_env() -> None:
    session = boto3.session.Session(region_name=FROM_SESSION)
    assert resolve_region(None, session) == FROM_SESSION
    assert aws_session(session, None) is session


def test_falls_back_to_boto3_chain_without_aws_region(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(REGION_ENV_VAR)
    assert resolve_region(None) is None
    assert aws_session(None, None).region_name == FROM_DEFAULT_ENV


def test_empty_values_are_ignored() -> None:
    assert resolve_region("", environ={REGION_ENV_VAR: ""}) is None


def test_lazy_client_honours_aws_region() -> None:
    assert LazyClient("dynamodb", region=None, session=None).get().meta.region_name == FROM_ENV


def test_events_gateway_honours_aws_region() -> None:
    assert BotoEventsGateway(None, None)._client("sts").meta.region_name == FROM_ENV


def test_events_gateway_resolves_its_session_once(monkeypatch: pytest.MonkeyPatch) -> None:
    created: list[boto3.session.Session] = []
    real_session = boto3.session.Session

    def counting_session(**kwargs: object) -> boto3.session.Session:
        created.append(real_session(**kwargs))
        return created[-1]

    monkeypatch.setattr(boto3.session, "Session", counting_session)
    gateway = BotoEventsGateway(None, None)
    gateway._client("sts")
    gateway._client("secretsmanager")
    assert len(created) == 1


def test_regionless_caller_session_falls_through_to_aws_region(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("AWS_DEFAULT_REGION")
    session = boto3.session.Session()
    assert session.region_name is None
    assert resolve_region(None, session) == FROM_ENV
    assert LazyClient("dynamodb", region=None, session=session).get().meta.region_name == FROM_ENV
    assert BotoEventsGateway(session, None)._client("sts").meta.region_name == FROM_ENV


def test_control_plane_honours_aws_region() -> None:
    plane = LambdaMicrovmsControlPlane.from_session(None)
    assert plane._client.meta.region_name == FROM_ENV


def test_volume_store_region_reports_aws_region() -> None:
    assert VolumeStore(file_system_id=FILE_SYSTEM_ID).region == FROM_ENV


def test_secret_store_region_reports_aws_region() -> None:
    assert SecretStore().region == FROM_ENV


def test_template_build_region_asks_for_aws_region(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(REGION_ENV_VAR)
    monkeypatch.delenv("AWS_DEFAULT_REGION")
    with pytest.raises(InvalidArgumentException, match=REGION_ENV_VAR):
        _ = _Clients(region=None, session=None).region


def test_template_build_region_honours_aws_region() -> None:
    assert _Clients(region=None, session=None).region == FROM_ENV
