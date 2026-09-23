"""Helpers puros del shim E2B 2.51 (`m9-e2b-v2-surface` D6-D8, D12, D14):
la tabla de `UnimplementedError`, la validación de `headers`/`proxy`/
`retries`, la mezcla de parámetros del cliente `E2B`, `ConnectionConfig`,
los filtros de `list()` y los JSON de los modelos."""

from __future__ import annotations

import json
import logging
import warnings
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

import pytest

import rayito.e2b._compat as compat
from rayito import CodeContext, ExecutionError, Logs
from rayito._aws import ClientSettings, LambdaMicrovmsControlPlane
from rayito._metrics_base import HISTORY_FEATURE, MetricsHistoryUnavailable
from rayito._models import SandboxMetrics as NativeSandboxMetrics
from rayito._transport import TransportSettings
from rayito.e2b import (
    AsyncSecret,
    AsyncTemplate,
    AsyncVolume,
    ConnectionConfig,
    RayitoCompatWarning,
    Sandbox,
    SandboxQuery,
    SandboxState,
    Secret,
    Template,
    UnimplementedError,
    Volume,
    get_signature,
)
from rayito.e2b._client import bind_class
from rayito.e2b._compat import (
    METRICS_HISTORY_IMAGE_REASON,
    history_falls_back_to_snapshot,
    list_mapping,
    native_call_kwargs,
    needs_metrics_snapshot,
    reject_callable_in_user_slot,
    resolve_metrics_token,
    validate_keep_memory,
    validate_on_resume,
)
from rayito.e2b._connection import (
    IGNORED_API_PARAMS,
    RESERVED_METADATA_KEYS,
    ConnectionSettings,
    connection_overrides,
    merge_bound_params,
    resolve_retries,
    split_api_params,
    validate_extra_headers,
    validate_proxy_url,
)
from rayito.e2b._unimplemented import UNIMPLEMENTED_REASONS, unimplemented
from rayito.exceptions import InvalidArgumentException, SandboxException

EVIDENCE = ("AWS_API_NOTES.md §", "SPEC.md §4")
IGNORED_VALUES: dict[str, Any] = {
    "api_key": "e2b_secreto",
    "domain": "e2b.dev",
    "debug": True,
    "api_url": "https://api.secreta",
    "sandbox_url": "https://sbx.secreta",
    "validate_api_key": True,
    "api_headers": {"k": "valor-secreto"},
}


@pytest.fixture(autouse=True)
def no_integration() -> Iterator[None]:
    ConnectionConfig.set_integration(None)
    yield
    ConnectionConfig.set_integration(None)


# ------------------------------------------------------------ unimplemented


@pytest.mark.parametrize("feature", sorted(UNIMPLEMENTED_REASONS))
def test_every_table_key_builds_its_error(feature: str) -> None:
    error = unimplemented(feature)
    assert isinstance(error, UnimplementedError)
    assert isinstance(error, NotImplementedError)
    assert not isinstance(error, SandboxException)
    assert error.feature == feature
    assert error.reason == UNIMPLEMENTED_REASONS[feature]
    assert any(evidence in error.reason for evidence in EVIDENCE)
    assert error.doc == "docs/site/docs/e2b-compat.md"
    assert "e2b-compat.md" in str(error)


def test_the_table_covers_d14() -> None:
    assert set(UNIMPLEMENTED_REASONS) == {
        "fork",
        "create_snapshot",
        "list_snapshots",
        "delete_snapshot",
        "connect(on_resume='reboot')",
        "pause(keep_memory=False)",
        "lifecycle.on_timeout.keep_memory=False",
        "network.rules",
        "network.mask_request_host",
        "network.allow_public_traffic=True",
        "iam",
        "mcp",
        "get_mcp_url",
        "get_mcp_token",
        "volume_mounts",
        "Volume",
        "get_signature",
        "Secret",
        "Template",
    }
    with pytest.raises(KeyError):
        unimplemented("no-such-feature")


RESOURCE_CALLS = [
    (Template, "build", "Template"),
    (Template, "to_dockerfile", "Template"),
    (AsyncTemplate, "build_in_background", "Template"),
    (Volume, "create", "Volume"),
    (AsyncVolume, "get_info", "Volume"),
    (Secret, "list", "Secret"),
    (AsyncSecret, "iam_token", "Secret"),
]


@pytest.mark.parametrize(("resource", "method", "feature"), RESOURCE_CALLS)
def test_resources_raise_unimplemented_never_attribute_error(
    resource: Any, method: str, feature: str
) -> None:
    with pytest.raises(UnimplementedError) as excinfo:
        getattr(resource, method)("x", alias="y")
    assert excinfo.value.feature == feature
    with pytest.raises(UnimplementedError):
        resource()


def test_get_signature_raises() -> None:
    with pytest.raises(UnimplementedError) as excinfo:
        get_signature("/home/user/a", "write", "user")
    assert excinfo.value.feature == "get_signature"


def test_on_resume_and_keep_memory_gates() -> None:
    validate_on_resume("restore")
    with pytest.raises(UnimplementedError) as excinfo:
        validate_on_resume("reboot")
    assert excinfo.value.feature == "connect(on_resume='reboot')"
    with pytest.raises(InvalidArgumentException):
        validate_on_resume("resume")
    validate_keep_memory(None)
    validate_keep_memory(True)
    with pytest.raises(UnimplementedError, match=r"pause\(keep_memory=False\)"):
        validate_keep_memory(False)


def test_a_callable_in_the_user_slot_is_refused() -> None:
    reject_callable_in_user_slot(None, "watch_dir")
    reject_callable_in_user_slot("root", "watch_dir")
    with pytest.raises(InvalidArgumentException, match="on_event"):
        reject_callable_in_user_slot(print, "watch_dir")
    with pytest.raises(InvalidArgumentException, match="on_data"):
        reject_callable_in_user_slot(print, "pty.create")


def test_class_metrics_token_names_both_sources() -> None:
    assert resolve_metrics_token("t", {}) == "t"
    assert resolve_metrics_token(None, {"RAYITO_ACCESS_TOKEN": "env"}) == "env"
    with pytest.raises(UnimplementedError) as excinfo:
        resolve_metrics_token(None, {})
    assert "access_token" in excinfo.value.reason
    assert "RAYITO_ACCESS_TOKEN" in excinfo.value.reason


# ------------------------------------------------------------------ headers


@pytest.mark.parametrize("key", sorted(RESERVED_METADATA_KEYS))
def test_reserved_header_keys_are_refused_naming_the_key(key: str) -> None:
    with pytest.raises(InvalidArgumentException) as excinfo:
        validate_extra_headers({key.upper(): "valor-secreto"})
    assert key in str(excinfo.value)
    assert "valor-secreto" not in str(excinfo.value)


@pytest.mark.parametrize(
    "key", ["x-aws-proxy-extra", "grpc-status", ":path", "trace-bin", "x trace", "x-ñ", ""]
)
def test_reserved_prefixes_suffix_and_non_tokens_are_refused(key: str) -> None:
    with pytest.raises(InvalidArgumentException, match="reservada"):
        validate_extra_headers({key: "1"})


def test_header_values_must_be_printable_ascii_and_keys_are_lowercased() -> None:
    with pytest.raises(InvalidArgumentException) as excinfo:
        validate_extra_headers({"x-trace": "valor\nsecreto"})
    assert "x-trace" in str(excinfo.value) and "secreto" not in str(excinfo.value)
    with pytest.raises(InvalidArgumentException):
        validate_extra_headers({"x-trace": "ñ"})
    assert validate_extra_headers({"X-Trace": "1", "x-b": "dos"}) == (
        ("x-trace", "1"),
        ("x-b", "dos"),
    )
    assert validate_extra_headers(None) == ()
    with pytest.raises(InvalidArgumentException):
        validate_extra_headers({"x-trace": 1})  # type: ignore[dict-item]


# -------------------------------------------------------------------- proxy


@pytest.mark.parametrize(
    "proxy", ["https://u:clave@h:3128", "http://u:clave@h", "u:clave@h:3128", "http://:3128", 3128]
)
def test_bad_proxies_are_refused_without_echoing_them(proxy: Any) -> None:
    with pytest.raises(InvalidArgumentException) as excinfo:
        validate_proxy_url(proxy)
    assert "clave" not in str(excinfo.value)


def test_http_proxy_with_userinfo_is_accepted() -> None:
    assert validate_proxy_url("http://u:p@h:3128") == "http://u:p@h:3128"
    assert validate_proxy_url(None) is None


@pytest.mark.parametrize("retries", [-1, True, 1.5, "2"])
def test_retries_bounds(retries: Any) -> None:
    with pytest.raises(InvalidArgumentException):
        resolve_retries(retries)


def test_retries_accepts_zero_and_none() -> None:
    assert resolve_retries(0) == 0
    assert resolve_retries(3) == 3
    assert resolve_retries(None) is None


# ------------------------------------------------------------- api params


def test_split_api_params_warns_once_per_ignored_param_without_values() -> None:
    settings, messages = split_api_params(IGNORED_VALUES, call="create")
    assert settings == ConnectionSettings()
    assert [message.split(" ")[0] for message in messages] == list(IGNORED_API_PARAMS)
    assert len(messages) == 7
    for secret in ("e2b_secreto", "e2b.dev", "https://", "valor-secreto"):
        assert not any(secret in message for message in messages)
    assert split_api_params({"debug": False}, call="create")[1] == ()
    assert len(compat.map_create_kwargs(secure=False).warnings) == 1


def test_split_api_params_applies_the_connection_keys() -> None:
    settings, messages = split_api_params(
        {"request_timeout": 5, "retries": 2, "headers": {"X-A": "1"}, "proxy": "http://h:1"},
        call="create",
    )
    assert messages == ()
    assert settings == ConnectionSettings(
        request_timeout=5, retries=2, headers=(("x-a", "1"),), proxy="http://h:1"
    )
    assert "http://h:1" not in repr(settings)


def test_unknown_api_params_are_the_python_type_error() -> None:
    with pytest.raises(TypeError, match=r"create\(\) got an unexpected keyword argument 'pool'"):
        split_api_params({"pool": object()}, call="create")


def test_merge_bound_params_follows_e2b() -> None:
    bound = {"region": "us-east-1", "headers": {"a": "1"}, "retries": 3}
    assert merge_bound_params(bound, {"region": None, "retries": 1}) == {
        "region": "us-east-1",
        "retries": 1,
        "headers": {"a": "1"},
    }
    assert merge_bound_params(bound, {"headers": {"b": "2"}})["headers"] == {"b": "2"}
    assert merge_bound_params({}, {"idle": None}) == {"idle": None}


def test_native_call_kwargs_merges_warns_and_resolves() -> None:
    call = native_call_kwargs(
        {"region": "us-east-1", "control_plane": "plane", "api_key": "bound"},
        {"region": None, "access_token": None},
        {"request_timeout": 7, "domain": "x"},
        call="get_info",
    )
    assert call.kwargs == {
        "region": "us-east-1",
        "access_token": None,
        "control_plane": "plane",
        "request_timeout": 7,
    }
    assert call.warnings == ("domain ignorado: el endpoint lo asigna Lambda MicroVMs por sandbox",)
    without = native_call_kwargs(
        {}, {"region": "r"}, {"request_timeout": 7}, call="kill", with_request_timeout=False
    )
    assert without.kwargs == {"region": "r"}
    with pytest.raises(TypeError, match="pool"):
        native_call_kwargs({}, {}, {"pool": 1}, call="create")


# -------------------------------------------------------- connection plane


def test_connection_overrides_without_settings_return_what_was_given() -> None:
    transport = TransportSettings()
    assert connection_overrides(
        ConnectionSettings(request_timeout=3),
        transport=transport,
        control_plane="plane",
        session=None,
        region=None,
        integration=None,
    ) == (transport, "plane")


def test_connection_overrides_build_the_transport_and_a_dedicated_plane() -> None:
    settings = ConnectionSettings(retries=2, headers=(("x-trace", "1"),), proxy="http://h:3128")
    transport, plane = connection_overrides(
        settings,
        transport=None,
        control_plane=None,
        session=None,
        region="us-east-1",
        integration="acme/1.0",
    )
    assert isinstance(transport, TransportSettings)
    assert transport.extra_metadata == (("x-trace", "1"),)
    assert transport.http_proxy == "http://h:3128"
    assert isinstance(plane, LambdaMicrovmsControlPlane)
    config = plane._client.meta.config
    assert config.retries["total_max_attempts"] == 3
    assert config.proxies == {"http": "http://h:3128", "https": "http://h:3128"}
    assert config.user_agent_extra.endswith("acme/1.0")
    again = connection_overrides(
        settings,
        transport=None,
        control_plane=None,
        session=None,
        region="us-east-1",
        integration="acme/1.0",
    )[1]
    assert again is plane
    assert ClientSettings(2, "http://h:3128", "acme/1.0") == ClientSettings(
        retries=2, proxy="http://h:3128", integration="acme/1.0"
    )


def test_settings_with_an_explicit_control_plane_are_refused() -> None:
    for settings, integration in (
        (ConnectionSettings(retries=1), None),
        (ConnectionSettings(proxy="http://h:1"), None),
        (ConnectionSettings(), "acme"),
    ):
        with pytest.raises(InvalidArgumentException, match="control_plane"):
            connection_overrides(
                settings,
                transport=None,
                control_plane="plane",
                session=None,
                region=None,
                integration=integration,
            )


# -------------------------------------------------------- ConnectionConfig


def test_connection_config_properties_and_validation() -> None:
    logger = logging.getLogger("app")
    config = ConnectionConfig(
        request_timeout=5, retries=1, headers={"X-A": "1"}, proxy="http://h:1", logger=logger
    )
    assert config.request_timeout == 5.0
    assert config.get_request_timeout() == 5.0
    assert config.get_request_timeout(2) == 2.0
    assert config.retries == 1
    assert dict(config.headers) == {"x-a": "1"}
    assert config.proxy == "http://h:1"
    assert config.logger is logger
    assert config.region is None
    assert "http://h:1" not in repr(config)
    assert ConnectionConfig().request_timeout == 60.0
    with pytest.raises(TypeError):
        config.headers["x-b"] = "2"  # type: ignore[index]
    with pytest.raises(InvalidArgumentException):
        ConnectionConfig(headers={"x-access-token": "t"})
    with pytest.raises(InvalidArgumentException):
        ConnectionConfig(proxy="socks5://h:1")


def test_connection_config_warns_for_ignored_keys() -> None:
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        ConnectionConfig(api_key="e2b_secreto", domain="e2b.dev")
    messages = [str(w.message) for w in caught if issubclass(w.category, RayitoCompatWarning)]
    assert [message.split(" ")[0] for message in messages] == ["api_key", "domain"]
    assert not any("e2b_secreto" in message for message in messages)


def test_set_integration_snapshot_and_validation() -> None:
    before = ConnectionConfig()
    ConnectionConfig.set_integration("acme/1.0")
    assert ConnectionConfig().integration == "acme/1.0"
    assert before.integration is None
    ConnectionConfig.set_integration(None)
    assert ConnectionConfig().integration is None
    for bad in ("", " acme", "acme /1.0", "acme/ 1.0", "añme"):
        with pytest.raises(InvalidArgumentException):
            ConnectionConfig.set_integration(bad)


# ----------------------------------------------------------------- listing


def test_list_mapping_merges_the_query_and_the_kwargs() -> None:
    after = datetime(2026, 9, 1, tzinfo=UTC)
    mapping = list_mapping(
        SandboxQuery(metadata={"a": "1"}, started_after=after, template="tpl"), None, None
    )
    assert mapping.states == ("RUNNING",)
    assert mapping.metadata == {"a": "1"}
    assert mapping.started_after == after
    assert mapping.template == "tpl"
    paused = list_mapping(SandboxQuery(state=[SandboxState.PAUSED]), None, "tpl")
    assert paused.states == ("SUSPENDING", "SUSPENDED")
    assert paused.template == "tpl"
    same = list_mapping(SandboxQuery(state=[SandboxState.RUNNING]), [SandboxState.RUNNING], None)
    assert same.states == ("PENDING", "RUNNING")
    assert list_mapping(None, None, None).states is None


def test_list_mapping_refuses_conflicting_filters() -> None:
    with pytest.raises(InvalidArgumentException, match=r"query\.state"):
        list_mapping(SandboxQuery(state=[SandboxState.PAUSED]), [SandboxState.RUNNING], None)
    with pytest.raises(InvalidArgumentException, match=r"query\.template"):
        list_mapping(SandboxQuery(template="a"), None, "b")
    with pytest.raises(UnimplementedError):
        list_mapping(SandboxQuery(metadata={"a": "1"}, state=[SandboxState.PAUSED]), None, None)


def test_sandbox_query_keeps_e2b_field_order() -> None:
    after = datetime(2026, 9, 1, tzinfo=UTC)
    query = SandboxQuery({"a": "1"}, [SandboxState.RUNNING], after, "tpl")
    assert (query.metadata, query.state, query.started_after, query.template) == (
        {"a": "1"},
        [SandboxState.RUNNING],
        after,
        "tpl",
    )


# -------------------------------------------------------------------- JSON


def test_json_round_trips() -> None:
    logs = Logs(stdout=["a"], stderr=["b"])
    assert json.loads(logs.to_json()) == {"stdout": ["a"], "stderr": ["b"]}
    error = ExecutionError("E", "v", "tb")
    assert json.loads(error.to_json()) == {"name": "E", "value": "v", "traceback": "tb"}
    context = CodeContext.from_json({"id": "c1", "language": "python", "cwd": "/home/user"})
    assert context == CodeContext("c1", "python", "/home/user")
    with pytest.raises(InvalidArgumentException, match="cwd"):
        CodeContext.from_json({"id": "c1", "language": "python"})


def test_bind_class_copies_the_params_it_binds() -> None:
    params: dict[str, Any] = {"region": "us-east-1", "headers": {"x-a": "1"}}
    bound = bind_class(Sandbox, params)
    params["region"] = "eu-west-1"
    params["retries"] = 3
    assert dict(bound._bound_params) == {"region": "us-east-1", "headers": {"x-a": "1"}}
    assert dict(Sandbox._bound_params) == {}
    assert issubclass(bound, Sandbox) and bound.__name__ == "Sandbox"


def test_history_on_a_pre_m9_image_falls_back_to_the_snapshot_without_range() -> None:
    unavailable = MetricsHistoryUnavailable(HISTORY_FEATURE, METRICS_HISTORY_IMAGE_REASON)
    assert history_falls_back_to_snapshot(unavailable, ranged=False)


def test_ranged_history_on_a_pre_m9_image_is_unimplemented() -> None:
    unavailable = MetricsHistoryUnavailable(HISTORY_FEATURE, METRICS_HISTORY_IMAGE_REASON)
    with pytest.raises(UnimplementedError) as excinfo:
        history_falls_back_to_snapshot(unavailable, ranged=True)
    assert excinfo.value.feature == "get_metrics(start=, end=)"
    assert excinfo.value.reason == METRICS_HISTORY_IMAGE_REASON
    assert excinfo.value.__cause__ is unavailable


def test_any_other_unimplemented_error_is_not_a_snapshot_fallback() -> None:
    other = UnimplementedError(HISTORY_FEATURE, METRICS_HISTORY_IMAGE_REASON)
    assert not history_falls_back_to_snapshot(other, ranged=False)
    assert not history_falls_back_to_snapshot(other, ranged=True)


def test_snapshot_is_needed_only_for_an_empty_unranged_series() -> None:
    sample = NativeSandboxMetrics(
        timestamp=datetime(2026, 1, 1, tzinfo=UTC),
        cpu_used_pct=1.0,
        cpu_count=2,
        mem_used_bytes=1,
        mem_total_bytes=2,
        disk_used_bytes=3,
        disk_total_bytes=4,
    )
    assert needs_metrics_snapshot([], ranged=False)
    assert not needs_metrics_snapshot([], ranged=True)
    assert not needs_metrics_snapshot([sample], ranged=False)
    assert not needs_metrics_snapshot([sample], ranged=True)


def test_e2b_value_objects_do_not_import_the_native_listing_adapters() -> None:
    import rayito.e2b._models as e2b_models

    assert not hasattr(e2b_models, "SandboxListPaginator")
    assert not hasattr(e2b_models, "AsyncSandboxListPaginator")
