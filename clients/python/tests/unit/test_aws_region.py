"""Resolución de la región: `region=` > sesión > `AWS_REGION` > cadena de boto3."""

from __future__ import annotations

import boto3
import pytest

from rayito._aws import LazyClient
from rayito._aws_region import REGION_ENV_VAR, aws_session, resolve_region
from rayito._lifecycle_events._aws import BotoEventsGateway
from rayito._s3 import S3Gateway
from rayito._secrets import SecretStore

EXPLICIT = "eu-west-1"
FROM_ENV = "us-west-2"
FROM_DEFAULT_ENV = "ap-south-1"
FROM_SESSION = "eu-central-1"


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


def test_s3_gateway_and_events_gateway_honour_aws_region() -> None:
    gateway = S3Gateway.from_session(None, EXPLICIT)
    assert gateway._client.meta.region_name == EXPLICIT
    assert BotoEventsGateway(None, None)._client("sts").meta.region_name == FROM_ENV


def test_secret_store_region_reports_aws_region() -> None:
    assert SecretStore().region == FROM_ENV
