"""La Lambda de la pila de punta a punta, sin red: un evento de Function URL
entra en `StackApp` y sale una respuesta. Cubre el orden de las comprobaciones
(autenticar antes de enrutar), los dos roles, la auditoría y las cabeceras."""

from __future__ import annotations

import base64
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import _stack_path  # noqa: F401
import jsonschema
import pytest
from dashboard_stack.app import MAX_JOB_SECONDS, ActionsUnavailable, StackApp
from dashboard_stack.appearance import LiveAppearance
from dashboard_stack.audit import AuditLog
from dashboard_stack.config import StackConfig
from dashboard_stack.jobs import InlineJobs, JobLog
from dashboard_stack.tokens import AuthUnavailable, Caller, TokenVerifier, signing_key_from_jwk
from stack_fakes import FakeControlPlane, FakeDynamo
from stack_signing import CLIENT_ID, ISSUER, KEY_ID, NOW, Signer

from rayito._dashboard._api._router import DashboardApi
from rayito._dashboard._cost import UsageCost
from rayito._dashboard._domain import BulkPlan
from rayito._dashboard._fleet import Fleet
from rayito._dashboard._quotas import Quotas
from rayito._dashboard._theme import Appearance, parse_logo, parse_theme
from rayito._limits import DASHBOARD_STACK_ACTIONS_GROUP

DOMAIN = "abc123.lambda-url.us-east-1.on.aws"
COGNITO_DOMAIN = "rayito-test.auth.us-east-1.amazoncognito.com"
IDS = tuple(f"microvm-00000000-0000-0000-0000-{n:012d}" for n in (1, 2))
ORIGIN = f"https://{DOMAIN}"


@pytest.fixture(scope="module")
def signer() -> Signer:
    return Signer()


class OneKey:
    def __init__(self, signer: Signer) -> None:
        self._key = signing_key_from_jwk(signer.jwk())

    def key(self, kid: str) -> Any:
        return self._key if kid == KEY_ID else None


class Clients:
    def __init__(self, *ids: str) -> None:
        self.ids = frozenset(ids or (CLIENT_ID,))

    def client_ids(self) -> frozenset[str]:
        return self.ids


class Gateway:
    """`ActionsGateway` falso: entrega la API con permiso de acciones."""

    def __init__(self, api: DashboardApi, *, fail: bool = False) -> None:
        self.api = api
        self.fail = fail
        self.callers: list[Caller] = []

    def api_for(self, caller: Caller) -> DashboardApi:
        self.callers.append(caller)
        if self.fail:
            raise ActionsUnavailable
        return self.api


class Rig:
    def __init__(self, signer: Signer, *, actions: bool = True, **kwargs: Any) -> None:
        self.signer = signer
        self.plane = FakeControlPlane(IDS)
        self.dynamo = FakeDynamo()
        jobs = JobLog()

        def api(allow_actions: bool) -> DashboardApi:
            fleet = Fleet(
                control_plane=self.plane, allow_actions=allow_actions, sleep=lambda _: None
            )
            return DashboardApi(
                fleet=fleet,
                quotas=Quotas(control_plane=self.plane),
                cost=UsageCost(control_plane=self.plane),
                region="us-east-1",
                jobs=InlineJobs(fleet, jobs),
            )

        self.read_api = api(False)
        self.gateway = Gateway(api(True), fail=kwargs.get("assume_fails", False))
        self.app = StackApp(
            config=StackConfig(
                region="us-east-1",
                user_pool_id="us-east-1_AbCdEfGhI",
                client_name="rayito-spa",
                cognito_domain=COGNITO_DOMAIN,
                audit_table="audit",
                cache_table="cache",
                actions_role_arn="arn:aws:iam::123456789012:role/actions" if actions else None,
                cost_explorer=False,
                theme_parameter=None,
                logo_parameter=None,
            ),
            verifier=TokenVerifier(
                issuer=ISSUER, keys=OneKey(signer), clients=Clients(), clock=lambda: NOW
            ),
            clients=Clients(),
            read_api=self.read_api,
            actions=self.gateway if actions else None,
            audit=AuditLog(self.dynamo, "audit"),
            appearance=LiveAppearance(lambda: kwargs.get("appearance", Appearance())),
            directory=kwargs.get("directory"),
        )

    def call(
        self,
        method: str,
        path: str,
        *,
        token: str | None = None,
        body: Any = None,
        headers: Mapping[str, str] | None = None,
        post_ok: bool = True,
    ) -> tuple[int, dict[str, str], Any]:
        sent = {name.lower(): value for name, value in (headers or {}).items()}
        if token is not None:
            sent["authorization"] = f"Bearer {token}"
        if method == "POST" and post_ok:
            sent.setdefault("origin", ORIGIN)
            sent.setdefault("content-type", "application/json")
        raw = json.dumps(body if body is not None else {}) if method == "POST" else ""
        event = {
            "rawPath": path,
            "rawQueryString": "",
            "headers": sent,
            "body": base64.b64encode(raw.encode()).decode(),
            "isBase64Encoded": True,
            "requestContext": {
                "domainName": DOMAIN,
                "requestId": "req-1",
                "http": {"method": method, "sourceIp": "203.0.113.7"},
            },
        }
        response = self.app.respond(event)
        text = response["body"]
        content_type = response["headers"]["Content-Type"]
        return (
            response["statusCode"],
            response["headers"],
            json.loads(text) if content_type.startswith("application/json") else text,
        )

    def token(self, *groups: str) -> str:
        return self.signer.token({"cognito:groups": list(groups)})


@pytest.fixture
def rig(signer: Signer) -> Rig:
    return Rig(signer)


KILL_ONE = f"/api/v1/sandboxes/{IDS[0]}/kill"


def test_the_shell_and_the_public_config_need_no_token_and_nothing_else_does(rig: Rig) -> None:
    status, _, page = rig.call("GET", "/")
    assert status == 200 and "<rayito-app>" in page
    status, _, config = rig.call("GET", "/config.json")
    assert config == {
        "auth": {
            "type": "cognito",
            "domain": f"https://{COGNITO_DOMAIN}",
            "clientId": CLIENT_ID,
            "scope": "openid",
        }
    }
    status, _, body = rig.call("GET", "/api/v1/meta")
    assert status == 401 and body["error"]["code"] == "unauthorized"
    assert rig.plane.listed == 0


def test_every_response_carries_the_security_headers_and_the_cognito_origin(rig: Rig) -> None:
    for path in ("/", "/api/v1/meta", "/missing"):
        _, headers, _ = rig.call("GET", path)
        policy = headers["Content-Security-Policy"]
        assert "script-src 'self'" in policy and "'unsafe-inline'" not in policy
        assert f"connect-src 'self' https://{COGNITO_DOMAIN}" in policy
        assert headers["Strict-Transport-Security"].startswith("max-age=")
        assert headers["Cache-Control"] == "no-store"
        assert headers["X-Content-Type-Options"] == "nosniff"


@pytest.mark.parametrize(
    "token", ["", "garbage", "a.b.c", None], ids=["empty", "garbage", "forged", "missing"]
)
def test_a_bad_token_is_a_401_that_never_reaches_aws(rig: Rig, token: str | None) -> None:
    status, _, body = rig.call("GET", "/api/v1/sandboxes", token=token)
    assert status == 401
    assert "token" in body["error"]["message"]
    assert rig.plane.listed == 0


def test_a_signed_token_reads_with_the_read_role(rig: Rig) -> None:
    status, _, body = rig.call("GET", "/api/v1/sandboxes", token=rig.token())
    assert status == 200
    assert {item["sandboxId"] for item in body["sandboxes"]} == set(IDS)
    assert rig.gateway.callers == []


def test_meta_tells_the_spa_whether_this_caller_may_act(rig: Rig) -> None:
    _, _, reader = rig.call("GET", "/api/v1/meta", token=rig.token())
    _, _, actor = rig.call("GET", "/api/v1/meta", token=rig.token(DASHBOARD_STACK_ACTIONS_GROUP))
    assert reader["allowActions"] is False and actor["allowActions"] is True


def test_meta_says_the_panel_runs_in_a_stack_so_help_shows_stack_hints(rig: Rig) -> None:
    _, _, meta = rig.call("GET", "/api/v1/meta", token=rig.token())
    assert meta["deployment"] == "stack"


def test_an_action_without_the_group_is_forbidden_and_touches_nothing(rig: Rig) -> None:
    status, _, body = rig.call("POST", KILL_ONE, token=rig.token(), body={"confirm": True})
    assert status == 403 and body["error"]["code"] == "forbidden"
    assert rig.plane.acted == [] and rig.dynamo.items == {} and rig.gateway.callers == []


def test_an_action_is_audited_before_it_runs_and_runs_with_the_actions_role(rig: Rig) -> None:
    status, _, body = rig.call(
        "POST", KILL_ONE, token=rig.token(DASHBOARD_STACK_ACTIONS_GROUP), body={"confirm": True}
    )
    assert status == 202 and body["job"]["status"] == "finished"
    assert rig.plane.acted == [("terminate_microvm", IDS[0])]
    assert [caller.subject for caller in rig.gateway.callers] == ["subject-0001"]
    kinds = sorted(row["kind"]["S"] for row in rig.dynamo.items.values())
    assert kinds == ["outcome", "requested"]
    requested = next(r for r in rig.dynamo.items.values() if r["kind"]["S"] == "requested")
    assert requested["subject"]["S"] == "subject-0001"
    assert requested["resource"]["S"] == IDS[0] and requested["action"]["S"] == "kill"


def test_a_bulk_action_writes_one_audit_row_per_sandbox(rig: Rig) -> None:
    status, _, _ = rig.call(
        "POST",
        "/api/v1/actions",
        token=rig.token(DASHBOARD_STACK_ACTIONS_GROUP),
        body={"action": "suspend", "sandboxIds": list(IDS), "confirm": True},
    )
    assert status == 202
    rows = [r for r in rig.dynamo.items.values() if r["kind"]["S"] == "requested"]
    assert sorted(r["resource"]["S"] for r in rows) == sorted(IDS)


def test_if_the_audit_cannot_be_written_the_action_does_not_run(rig: Rig) -> None:
    rig.dynamo.fail = True
    status, _, body = rig.call(
        "POST", KILL_ONE, token=rig.token(DASHBOARD_STACK_ACTIONS_GROUP), body={"confirm": True}
    )
    assert status == 503 and body["error"]["code"] == "audit_unavailable"
    assert rig.plane.acted == []


def test_a_job_that_cannot_finish_inside_the_function_is_refused_before_the_audit(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    too_long = BulkPlan("kill", "TerminateMicrovm", len(IDS), 0.01, MAX_JOB_SECONDS + 1.0)
    monkeypatch.setattr(rig.read_api, "plan_for", lambda action, count: too_long)
    status, _, body = rig.call(
        "POST",
        "/api/v1/actions",
        token=rig.token(DASHBOARD_STACK_ACTIONS_GROUP),
        body={"action": "kill", "sandboxIds": list(IDS), "confirm": True},
    )
    assert status == 400 and body["error"]["code"] == "invalid_argument"
    assert "elige menos sandboxes" in body["error"]["message"]
    assert rig.plane.acted == [] and rig.dynamo.items == {} and rig.gateway.callers == []


def test_a_job_that_just_fits_the_function_runs(rig: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    fits = BulkPlan("kill", "TerminateMicrovm", len(IDS), 1.0, float(MAX_JOB_SECONDS))
    monkeypatch.setattr(rig.read_api, "plan_for", lambda action, count: fits)
    status, _, _ = rig.call(
        "POST",
        "/api/v1/actions",
        token=rig.token(DASHBOARD_STACK_ACTIONS_GROUP),
        body={"action": "kill", "sandboxIds": list(IDS), "confirm": True},
    )
    assert status == 202


def test_an_action_invalidates_the_read_cache_exactly_once(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []
    monkeypatch.setattr(rig.read_api, "invalidate", lambda: calls.append("invalidate"))
    rig.call(
        "POST", KILL_ONE, token=rig.token(DASHBOARD_STACK_ACTIONS_GROUP), body={"confirm": True}
    )
    assert calls == ["invalidate"]


def test_an_unconfirmed_action_is_refused_before_any_audit_row(rig: Rig) -> None:
    status, _, body = rig.call(
        "POST", KILL_ONE, token=rig.token(DASHBOARD_STACK_ACTIONS_GROUP), body={}
    )
    assert status == 400 and body["error"]["code"] == "confirmation_required"
    assert rig.dynamo.items == {}


class Directory:
    def __init__(self, *, allowed: bool = True, unavailable: bool = False) -> None:
        self.allowed = allowed
        self.unavailable = unavailable
        self.asked: list[str] = []

    def may_act(self, caller: Caller) -> bool:
        self.asked.append(caller.subject)
        if self.unavailable:
            raise AuthUnavailable("directory")
        return self.allowed


def test_a_user_disabled_or_removed_from_the_group_cannot_act_with_a_valid_token(
    signer: Signer,
) -> None:
    rig = Rig(signer, directory=Directory(allowed=False))
    status, _, body = rig.call(
        "POST", KILL_ONE, token=rig.token(DASHBOARD_STACK_ACTIONS_GROUP), body={"confirm": True}
    )
    assert status == 403 and body["error"]["code"] == "forbidden"
    assert rig.plane.acted == [] and rig.dynamo.items == {} and rig.gateway.callers == []


def test_if_the_directory_cannot_answer_the_action_does_not_run(signer: Signer) -> None:
    rig = Rig(signer, directory=Directory(unavailable=True))
    status, _, body = rig.call(
        "POST", KILL_ONE, token=rig.token(DASHBOARD_STACK_ACTIONS_GROUP), body={"confirm": True}
    )
    assert status == 503 and body["error"]["code"] == "auth_unavailable"
    assert rig.plane.acted == [] and rig.dynamo.items == {}


def test_reading_never_asks_the_directory(signer: Signer) -> None:
    directory = Directory()
    rig = Rig(signer, directory=directory)
    assert rig.call("GET", "/api/v1/sandboxes", token=rig.token())[0] == 200
    assert directory.asked == []


def test_a_refused_assume_role_is_reported_as_aws_access_denied(signer: Signer) -> None:
    rig = Rig(signer, assume_fails=True)
    status, _, body = rig.call(
        "POST", KILL_ONE, token=rig.token(DASHBOARD_STACK_ACTIONS_GROUP), body={"confirm": True}
    )
    assert status == 403 and body["error"]["code"] == "aws_access_denied"
    assert rig.plane.acted == []


def test_a_stack_without_actions_refuses_them_even_for_the_group(signer: Signer) -> None:
    rig = Rig(signer, actions=False)
    status, _, body = rig.call(
        "POST", KILL_ONE, token=rig.token(DASHBOARD_STACK_ACTIONS_GROUP), body={"confirm": True}
    )
    assert status == 403 and body["error"]["code"] == "actions_disabled"
    _, _, meta = rig.call("GET", "/api/v1/meta", token=rig.token(DASHBOARD_STACK_ACTIONS_GROUP))
    assert meta["allowActions"] is False


def test_a_post_from_another_origin_or_without_json_is_refused(rig: Rig) -> None:
    token = rig.token(DASHBOARD_STACK_ACTIONS_GROUP)
    status, _, body = rig.call(
        "POST", KILL_ONE, token=token, headers={"origin": "https://evil.example.com"}
    )
    assert status == 403
    status, _, body = rig.call(
        "POST", KILL_ONE, token=token, headers={"content-type": "text/plain"}
    )
    assert status == 415 and body["error"]["code"] == "unsupported_media_type"
    status, _, _ = rig.call("POST", KILL_ONE, token=token, post_ok=False)
    assert status == 403
    assert rig.plane.acted == []


def test_other_methods_and_unknown_paths_are_refused(rig: Rig) -> None:
    assert rig.call("DELETE", "/api/v1/sandboxes", token=rig.token())[0] == 405
    assert rig.call("POST", "/", token=rig.token())[0] == 405
    assert rig.call("GET", "/etc/passwd")[0] == 404
    assert rig.call("GET", "/js/../../handler.py")[0] == 404


def test_a_theme_is_served_with_its_name_and_a_logo_with_a_closed_csp(signer: Signer) -> None:
    theme = parse_theme(json.dumps({"version": 1, "name": "mi-marca"}), fmt="json")
    rig = Rig(signer, appearance=Appearance(theme=theme))
    _, headers, css = rig.call("GET", "/theme.css")
    assert "mi-marca" in css and headers["Content-Type"].startswith("text/css")
    _, _, meta = rig.call("GET", "/api/v1/meta", token=rig.token())
    assert meta["theme"] == {"name": "mi-marca", "logo": False}
    svg = b'<svg xmlns="http://www.w3.org/2000/svg"><rect width="1" height="1"/></svg>'
    with_logo = Rig(signer, appearance=Appearance(logo=parse_logo(svg)))
    status, headers, _ = with_logo.call("GET", "/theme/logo")
    assert status == 200 and headers["Content-Type"] == "image/svg+xml"
    assert headers["Content-Security-Policy"].endswith("sandbox")


def test_an_unexpected_error_is_an_opaque_500_without_detail(rig: Rig) -> None:
    def explode(*_: object, **__: object) -> Any:
        raise RuntimeError("secret detail")

    rig.read_api.handle = explode  # type: ignore[method-assign]
    status, _, body = rig.call("GET", "/api/v1/sandboxes", token=rig.token())
    assert status == 500 and "secret detail" not in json.dumps(body)


SCHEMA = json.loads(
    (
        Path(__file__).resolve().parents[4] / "testdata" / "dashboard" / "api-v1.schema.json"
    ).read_text(encoding="utf-8")
)


def conforms(document: Any, definition: str) -> None:
    jsonschema.validate(document, {"$ref": f"#/$defs/{definition}", "$defs": SCHEMA["$defs"]})


def test_the_stack_answers_with_the_same_contract_as_the_local_server(rig: Rig) -> None:
    token = rig.token(DASHBOARD_STACK_ACTIONS_GROUP)
    conforms(rig.call("GET", "/api/v1/meta", token=token)[2], "meta")
    conforms(rig.call("GET", "/api/v1/sandboxes", token=token)[2], "sandboxes")
    conforms(rig.call("GET", "/api/v1/sandboxes", token=None)[2], "error")
    status, _, job = rig.call("POST", KILL_ONE, token=token, body={"confirm": True})
    assert status == 202
    conforms(job, "jobDocument")
    conforms(
        rig.call("GET", f"/api/v1/actions/{job['job']['jobId']}", token=token)[2], "jobDocument"
    )
    rig.dynamo.fail = True
    conforms(rig.call("POST", KILL_ONE, token=token, body={"confirm": True})[2], "error")
