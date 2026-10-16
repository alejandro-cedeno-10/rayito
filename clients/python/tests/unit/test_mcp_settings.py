"""`McpSettings.from_env`: valores por defecto, cada variable, los `ValueError`
que nombran la variable y la restricción idle < timeout (design D4)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Final

import pytest

from rayito import IdlePolicy
from rayito._limits import IDLE_MAX_IDLE_MIN_SECONDS, MAX_DURATION_SECONDS
from rayito._sandbox_base import TEMPLATE_ENV_VAR
from rayito.mcp._settings import (
    DEFAULT_IDLE_SECONDS,
    DEFAULT_LOG_LEVEL,
    DEFAULT_TIMEOUT_SECONDS,
    EXECUTION_ROLE_ENV_VAR,
    IDLE_ENV_VAR,
    LOG_LEVEL_ENV_VAR,
    MIN_TIMEOUT_SECONDS,
    TEMPLATE_VERSION_ENV_VAR,
    TIMEOUT_ENV_VAR,
    McpSettings,
)

REPO_ROOT_DEPTH: Final = 4
SETTINGS_CASES: Final = (
    Path(__file__).resolve().parents[REPO_ROOT_DEPTH] / "testdata" / "mcp" / "settings-cases.json"
)
SHARED: Final = json.loads(SETTINGS_CASES.read_text(encoding="utf-8"))
ROLE_ARN = "arn:aws:iam::123456789012:role/rayito-execution"


def test_defaults() -> None:
    settings = McpSettings.from_env({})
    assert settings.template is None
    assert settings.template_version is None
    assert settings.execution_role_arn is None
    assert settings.timeout_seconds == DEFAULT_TIMEOUT_SECONDS == 3600
    assert settings.idle_seconds == DEFAULT_IDLE_SECONDS == 300
    assert settings.log_level == "INFO"
    assert settings.logging == "disabled"
    assert settings.idle_policy == IdlePolicy(max_idle_seconds=300, auto_resume=True)


def test_every_variable_is_read() -> None:
    settings = McpSettings.from_env(
        {
            TEMPLATE_ENV_VAR: "rayito-base",
            TEMPLATE_VERSION_ENV_VAR: "16.0",
            EXECUTION_ROLE_ENV_VAR: ROLE_ARN,
            TIMEOUT_ENV_VAR: "900",
            IDLE_ENV_VAR: "120",
            LOG_LEVEL_ENV_VAR: "debug",
        }
    )
    assert settings.template == "rayito-base"
    assert settings.template_version == "16.0"
    assert settings.execution_role_arn == ROLE_ARN
    assert settings.timeout_seconds == 900
    assert settings.idle_seconds == 120
    assert settings.log_level == "DEBUG"
    assert settings.logging == "cloudwatch"
    assert settings.idle_policy == IdlePolicy(max_idle_seconds=120, auto_resume=True)


def test_empty_values_fall_back_to_defaults() -> None:
    settings = McpSettings.from_env(
        {TEMPLATE_ENV_VAR: "", TIMEOUT_ENV_VAR: "", IDLE_ENV_VAR: "", LOG_LEVEL_ENV_VAR: ""}
    )
    assert settings == McpSettings()


def test_idle_zero_disables_auto_suspend() -> None:
    settings = McpSettings.from_env({IDLE_ENV_VAR: "0"})
    assert settings.idle_seconds == 0
    assert settings.idle_policy is None


def test_timeout_bounds_are_inclusive() -> None:
    assert McpSettings.from_env({TIMEOUT_ENV_VAR: "60", IDLE_ENV_VAR: "0"}).timeout_seconds == 60
    assert McpSettings.from_env({TIMEOUT_ENV_VAR: "28800"}).timeout_seconds == 28800
    assert McpSettings.from_env({IDLE_ENV_VAR: "60"}).idle_seconds == 60


def test_short_timeout_is_valid_when_idle_is_below_it_or_disabled() -> None:
    assert McpSettings.from_env({TIMEOUT_ENV_VAR: "120", IDLE_ENV_VAR: "60"}).idle_seconds == 60
    assert McpSettings.from_env({TIMEOUT_ENV_VAR: "120", IDLE_ENV_VAR: "0"}).idle_policy is None


@pytest.mark.parametrize(
    ("environ", "variable"),
    [
        ({IDLE_ENV_VAR: "abc"}, IDLE_ENV_VAR),
        ({TIMEOUT_ENV_VAR: "30"}, TIMEOUT_ENV_VAR),
        ({TIMEOUT_ENV_VAR: "28801"}, TIMEOUT_ENV_VAR),
        ({TIMEOUT_ENV_VAR: "1h"}, TIMEOUT_ENV_VAR),
        ({IDLE_ENV_VAR: "10"}, IDLE_ENV_VAR),
        ({IDLE_ENV_VAR: "-60"}, IDLE_ENV_VAR),
        ({LOG_LEVEL_ENV_VAR: "LOUD"}, LOG_LEVEL_ENV_VAR),
        ({TIMEOUT_ENV_VAR: "120"}, IDLE_ENV_VAR),
        ({TIMEOUT_ENV_VAR: "300"}, IDLE_ENV_VAR),
        ({TIMEOUT_ENV_VAR: "600", IDLE_ENV_VAR: "900"}, IDLE_ENV_VAR),
    ],
)
def test_malformed_values_name_the_variable(environ: dict[str, str], variable: str) -> None:
    with pytest.raises(ValueError, match=variable):
        McpSettings.from_env(environ)


def test_idle_not_below_timeout_names_both_variables() -> None:
    expected = f"{IDLE_ENV_VAR}=300 debe ser menor que {TIMEOUT_ENV_VAR}=120"
    with pytest.raises(ValueError, match=expected):
        McpSettings.from_env({TIMEOUT_ENV_VAR: "120"})
    with pytest.raises(ValueError, match=IDLE_ENV_VAR):
        McpSettings(timeout_seconds=600, idle_seconds=600)


def test_defaults_and_bounds_match_the_shared_contract() -> None:
    defaults = SHARED["defaults"]
    assert defaults["timeoutSeconds"] == DEFAULT_TIMEOUT_SECONDS
    assert defaults["idleSeconds"] == DEFAULT_IDLE_SECONDS
    assert defaults["minTimeoutSeconds"] == MIN_TIMEOUT_SECONDS
    assert defaults["maxTimeoutSeconds"] == MAX_DURATION_SECONDS
    assert defaults["minIdleSeconds"] == IDLE_MAX_IDLE_MIN_SECONDS
    assert defaults["logLevel"] == DEFAULT_LOG_LEVEL


def describe_settings(settings: McpSettings) -> dict[str, Any]:
    policy = settings.idle_policy
    return {
        "template": settings.template,
        "templateVersion": settings.template_version,
        "executionRoleArn": settings.execution_role_arn,
        "timeoutSeconds": settings.timeout_seconds,
        "idleSeconds": settings.idle_seconds,
        "logLevel": settings.log_level,
        "logging": settings.logging,
        "idlePolicy": (
            None
            if policy is None
            else {"maxIdleSeconds": policy.max_idle_seconds, "autoResume": policy.auto_resume}
        ),
    }


@pytest.mark.parametrize("case", SHARED["cases"], ids=[case["name"] for case in SHARED["cases"]])
def test_shared_settings_cases(case: dict[str, Any]) -> None:
    if "errorVariable" in case:
        with pytest.raises(ValueError, match=case["errorVariable"]):
            McpSettings.from_env(case["env"])
        return
    assert describe_settings(McpSettings.from_env(case["env"])) == case["expected"]
