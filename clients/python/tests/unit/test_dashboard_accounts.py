"""La vista multicuenta del panel local (opt-in): las especificaciones
`--account`, el reparto de `AccountsApi` por `?account=`, que las cuentas
adicionales son de sólo lectura, y la sesión que asume el rol."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import boto3
import pytest
from botocore.exceptions import ClientError

from rayito._dashboard._accounts import AccountsApi, parse_account_specs
from rayito._dashboard._api._contract import API_PREFIX, ApiRequest
from rayito._dashboard._assume import AssumedRole, assumed_session
from rayito._dashboard._theme import Branding
from rayito._limits import (
    DASHBOARD_ACCOUNT_LABEL_MAX_CHARS,
    DASHBOARD_ACCOUNTS_MAX,
    DASHBOARD_ASSUME_ROLE_SESSION_NAME,
    DASHBOARD_ASSUME_ROLE_SESSION_SECONDS,
)
from rayito.cli._session import Clients
from rayito.cli.dashboard import with_accounts
from rayito.exceptions import AuthenticationException, InvalidArgumentException

from .dashboard_support import Rig, validate
from .fake_dashboard import FakePlane, list_item

pytestmark = pytest.mark.usefixtures("clean_catalog")

ROLE_ARN = "arn:aws:iam::123456789012:role/rayito-dashboard-read"
OTHER_ROLE_ARN = "arn:aws:iam::123456789012:role/rayito-dashboard-read-other"
WRITES = {"terminate_microvm", "suspend_microvm", "resume_microvm"}


def request(method: str, path: str, **query: str) -> ApiRequest:
    return ApiRequest(method, f"{API_PREFIX}{path}", {k: [v] for k, v in query.items()})


def two_accounts() -> tuple[AccountsApi, Rig, Rig]:
    primary = Rig(FakePlane([list_item(1)]), allow_actions=True)
    staging = Rig(FakePlane([list_item(2), list_item(3)]))
    return AccountsApi(primary.api, {"staging": staging.api}), primary, staging


def test_specs_pair_a_label_with_a_role_arn() -> None:
    specs = parse_account_specs([f"staging={ROLE_ARN}", f"Prod 2={OTHER_ROLE_ARN}"])
    assert [(spec.label, spec.role_arn) for spec in specs] == [
        ("staging", ROLE_ARN),
        ("Prod 2", OTHER_ROLE_ARN),
    ]


@pytest.mark.parametrize(
    "value",
    [
        "staging",
        f"={ROLE_ARN}",
        f"-bad={ROLE_ARN}",
        f"{'a' * 33}={ROLE_ARN}",
        "staging=arn:aws:iam::123456789012:root",
        "staging=arn:aws:iam::123456789012:user/someone",
        "staging=123456789012",
    ],
)
def test_malformed_specs_are_refused_without_echoing_the_value(value: str) -> None:
    with pytest.raises(InvalidArgumentException) as raised:
        parse_account_specs([value])
    assert value not in str(raised.value)


def test_labels_are_unique_ignoring_case_and_capped() -> None:
    with pytest.raises(InvalidArgumentException):
        parse_account_specs([f"staging={ROLE_ARN}", f"STAGING={OTHER_ROLE_ARN}"])
    many = [f"a{n}={ROLE_ARN}" for n in range(DASHBOARD_ACCOUNTS_MAX + 1)]
    with pytest.raises(InvalidArgumentException):
        parse_account_specs(many)
    assert len(parse_account_specs(many[:DASHBOARD_ACCOUNTS_MAX])) == DASHBOARD_ACCOUNTS_MAX


def test_the_spec_repr_hides_the_role_arn() -> None:
    (spec,) = parse_account_specs([f"staging={ROLE_ARN}"])
    assert "123456789012" not in repr(spec)


def test_the_account_list_has_labels_only() -> None:
    api, _, _ = two_accounts()
    response = api.handle(request("GET", "/accounts"))
    assert response.status == 200
    validate(response.body, "accounts")
    assert response.body == {"accounts": [{"name": "staging"}]}


def test_without_the_parameter_the_primary_account_answers() -> None:
    api, _, staging = two_accounts()
    body = api.handle(request("GET", "/sandboxes")).body
    assert len(body["sandboxes"]) == 1
    assert staging.plane.operations() == []


def test_the_parameter_selects_another_account_and_is_not_forwarded() -> None:
    api, primary, _ = two_accounts()
    body = api.handle(request("GET", "/sandboxes", account="staging")).body
    assert len(body["sandboxes"]) == 2
    assert primary.plane.operations() == []
    meta = api.handle(request("GET", "/meta", account="staging")).body
    assert meta["allowActions"] is False


def test_an_unknown_account_is_refused_before_any_aws_call() -> None:
    api, primary, staging = two_accounts()
    response = api.handle(request("GET", "/sandboxes", account="nope"))
    assert response.status == 400
    assert response.body["error"]["code"] == "invalid_argument"
    assert primary.plane.operations() == [] and staging.plane.operations() == []


def test_additional_accounts_never_run_actions() -> None:
    api, primary, staging = two_accounts()
    response = api.handle(
        ApiRequest(
            "POST",
            f"{API_PREFIX}/sandboxes/{'x'}/kill",
            {"account": ["staging"]},
            b'{"confirm": true}',
        )
    )
    assert response.status == 403
    assert response.body["error"]["code"] == "actions_disabled"
    assert "sólo lectura" in response.body["error"]["message"]
    assert "--allow-actions" not in response.body["error"]["message"]
    assert not [
        op for op in staging.plane.operations() + primary.plane.operations() if op in WRITES
    ]


def test_csrf_rules_are_the_primary_ones_and_close_closes_everything() -> None:
    api, _, _ = two_accounts()
    assert api.requires_csrf(request("POST", "/actions")) is True
    assert api.requires_csrf(request("GET", "/sandboxes")) is False
    api.close()


class FakeSts:
    def __init__(self, *, fail: str | None = None, expiration: Any = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self._fail = fail
        self._expiration = expiration or datetime(2099, 1, 1, tzinfo=UTC)

    def assume_role(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        if self._fail:
            raise ClientError(
                {"Error": {"Code": self._fail, "Message": f"no puedes asumir {ROLE_ARN}"}},
                "AssumeRole",
            )
        return {
            "Credentials": {
                "AccessKeyId": "AKIAEXAMPLE",
                "SecretAccessKey": "secret",
                "SessionToken": "token",
                "Expiration": self._expiration,
            }
        }


def test_assuming_a_role_asks_sts_with_the_named_session_and_the_external_id() -> None:
    sts = FakeSts()
    fetched = AssumedRole(sts, role_arn=ROLE_ARN, external_id="ext-1").fetch()
    assert sts.calls == [
        {
            "RoleArn": ROLE_ARN,
            "RoleSessionName": DASHBOARD_ASSUME_ROLE_SESSION_NAME,
            "DurationSeconds": DASHBOARD_ASSUME_ROLE_SESSION_SECONDS,
            "ExternalId": "ext-1",
        }
    ]
    assert fetched["access_key"] == "AKIAEXAMPLE"


@pytest.mark.parametrize("expiration", ["2099-01-01T00:00:00Z", 4070908800, object()])
def test_credentials_without_a_real_expiry_date_are_an_authentication_error(
    expiration: Any,
) -> None:
    with pytest.raises(AuthenticationException) as raised:
        AssumedRole(FakeSts(expiration=expiration), role_arn=ROLE_ARN).fetch()
    assert "caducidad" in str(raised.value)


def test_without_an_external_id_none_is_sent() -> None:
    sts = FakeSts()
    AssumedRole(sts, role_arn=ROLE_ARN).fetch()
    assert "ExternalId" not in sts.calls[0]


def test_building_the_session_makes_no_sts_call_until_it_signs() -> None:
    sts = FakeSts()
    session = assumed_session(AssumedRole(sts, role_arn=ROLE_ARN), region="us-east-1")
    assert sts.calls == []
    frozen = session.get_credentials().get_frozen_credentials()
    assert (frozen.access_key, len(sts.calls)) == ("AKIAEXAMPLE", 1)
    assert session.region_name == "us-east-1"


def test_a_rejected_assume_role_keeps_the_aws_code_and_drops_the_message() -> None:
    with pytest.raises(AuthenticationException) as raised:
        AssumedRole(FakeSts(fail="AccessDenied"), role_arn=ROLE_ARN).fetch()
    assert raised.value.aws_code == "AccessDenied"
    assert "123456789012" not in str(raised.value)
    assert raised.value.__cause__ is None and raised.value.__suppress_context__


def test_every_account_reports_the_branding_of_the_primary_one() -> None:
    clients = Clients(
        session=boto3.session.Session(region_name="us-east-1"),
        region="us-east-1",
        clients={"sts": FakeSts()},
    )
    branding = Branding(name="acme", logo=True)
    primary = Rig(FakePlane([list_item(1)]), branding=branding)
    specs = parse_account_specs([f"staging={ROLE_ARN}"])
    api = with_accounts(clients, primary.api, specs, None, branding)
    own = api.handle(request("GET", "/meta")).body["theme"]
    other = api.handle(request("GET", "/meta", account="staging")).body["theme"]
    assert own == other == {"name": "acme", "logo": True}


def test_the_label_limit_is_the_named_one() -> None:
    longest = "a" * DASHBOARD_ACCOUNT_LABEL_MAX_CHARS
    assert parse_account_specs([f"{longest}={ROLE_ARN}"])[0].label == longest
    with pytest.raises(InvalidArgumentException):
        parse_account_specs([f"{longest}a={ROLE_ARN}"])
