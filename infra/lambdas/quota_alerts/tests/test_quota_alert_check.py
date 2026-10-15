"""La decisión, el ciclo de la comprobación, los adaptadores y la
configuración de la Lambda de alertas de cuota."""

from __future__ import annotations

import _alerts_path
import pytest
from alert_fakes import (
    FakeClient,
    FakeMetrics,
    FakeNotifier,
    FakeQuotas,
    FakeState,
    report_for,
    report_without_quota,
)
from quota_alert_check.adapters import (
    CloudWatchMetricSink,
    DynamoStateStore,
    SnsNotifier,
)
from quota_alert_check.config import AlertConfig, ConfigError
from quota_alert_check.decision import (
    SUBJECT_MAX_CHARS,
    Level,
    body,
    classify,
    level_to_remember,
    parse_level,
    should_notify,
    subject,
)
from quota_alert_check.runner import QuotaAlertCheck

from rayito._limits import (
    QUOTA_CRITICAL_FRACTION,
    QUOTA_METRIC_NAME,
    QUOTA_METRIC_NAMESPACE,
    QUOTA_WARNING_FRACTION,
)

REGION = "us-east-1"
TOPIC = "arn:aws:sns:us-east-1:123456789012:rayito-quota-alerts"
ENVIRON = {
    "AWS_REGION": REGION,
    "RAYITO_ALERTS_TOPIC_ARN": TOPIC,
    "RAYITO_ALERTS_STATE_TABLE": "rayito-quota-alerts-state",
}
QUOTA_GB = 1000.0
ABOVE_WARNING_GB = QUOTA_GB * (QUOTA_WARNING_FRACTION + 0.05)
ABOVE_CRITICAL_GB = QUOTA_GB * (QUOTA_CRITICAL_FRACTION + 0.02)
BELOW_WARNING_GB = QUOTA_GB * (QUOTA_WARNING_FRACTION - 0.3)


def build_check(
    allocated_gb: float,
    state: FakeState,
    notifier: FakeNotifier | None = None,
    metrics: FakeMetrics | None = None,
    **report_kwargs: int,
) -> QuotaAlertCheck:
    return QuotaAlertCheck(
        config=AlertConfig.from_environ(ENVIRON),
        quotas=FakeQuotas(report_for(allocated_gb, QUOTA_GB, **report_kwargs)),
        state=state,
        notifier=notifier or FakeNotifier(),
        metrics=metrics or FakeMetrics(),
    )


@pytest.mark.parametrize(
    ("fraction", "expected"),
    [
        (None, "ok"),
        (0.0, "ok"),
        (0.79, "ok"),
        (0.8, "warning"),
        (0.949, "warning"),
        (0.95, "critical"),
        (2.0, "critical"),
    ],
)
def test_classify_uses_the_given_thresholds(fraction: float | None, expected: Level) -> None:
    assert classify(fraction, 0.8, 0.95) == expected


def test_classify_honours_custom_thresholds() -> None:
    assert classify(0.6, 0.5, 0.7) == "warning"


@pytest.mark.parametrize(
    ("previous", "current", "expected"),
    [
        ("ok", "warning", True),
        ("ok", "critical", True),
        ("warning", "critical", True),
        ("warning", "warning", False),
        ("critical", "critical", False),
        ("critical", "warning", False),
        ("warning", "ok", False),
        ("ok", "ok", False),
    ],
)
def test_only_a_rising_level_notifies(previous: Level, current: Level, expected: bool) -> None:
    assert should_notify(previous, current) is expected


def test_an_unknown_stored_level_never_silences_an_alert() -> None:
    assert parse_level("garbage") == "ok"
    assert parse_level(None) == "ok"
    assert parse_level("critical") == "critical"


def test_the_message_names_region_and_figures_and_nothing_identifying() -> None:
    text = body("warning", REGION, 850.0, 1000.0, 12, 0)
    assert REGION in text and "850" in text and "1000" in text and "12 sandboxes" in text
    assert "No se pudo leer" not in text
    assert "No se pudo leer" in body("warning", REGION, 850.0, 1000.0, 12, 3)
    assert len(subject("critical", REGION, 0.97)) <= SUBJECT_MAX_CHARS
    assert "97 %" in subject("critical", REGION, 0.97)


def test_crossing_the_warning_threshold_notifies_once_then_stays_quiet() -> None:
    state, notifier = FakeState(), FakeNotifier()
    check = build_check(ABOVE_WARNING_GB, state, notifier)
    first = check.run()
    second = check.run()
    assert (first.level, first.notified) == ("warning", True)
    assert (second.level, second.notified) == ("warning", False)
    assert len(notifier.sent) == 1


def test_going_from_warning_to_critical_notifies_again() -> None:
    state, notifier = FakeState("warning"), FakeNotifier()
    result = build_check(ABOVE_CRITICAL_GB, state, notifier).run()
    assert (result.level, result.notified) == ("critical", True)
    assert state.saved == ["critical"]


def test_dropping_below_the_threshold_resets_so_the_next_cross_notifies() -> None:
    state, notifier = FakeState("critical"), FakeNotifier()
    build_check(BELOW_WARNING_GB, state, notifier).run()
    assert state.level == "ok"
    build_check(ABOVE_WARNING_GB, state, notifier).run()
    assert len(notifier.sent) == 1


def test_oscillating_around_the_critical_threshold_notifies_only_once() -> None:
    state, notifier = FakeState(), FakeNotifier()
    for allocated_gb in (ABOVE_CRITICAL_GB, ABOVE_WARNING_GB, ABOVE_CRITICAL_GB, ABOVE_WARNING_GB):
        build_check(allocated_gb, state, notifier).run()
    assert len(notifier.sent) == 1
    assert state.level == "critical"


def test_the_highest_level_is_kept_until_the_usage_is_ok_again() -> None:
    assert level_to_remember("critical", "warning") == "critical"
    assert level_to_remember("warning", "critical") == "critical"
    assert level_to_remember("ok", "warning") == "warning"
    assert level_to_remember("critical", "ok") == "ok"


def test_after_dropping_to_ok_a_new_critical_crossing_notifies_again() -> None:
    state, notifier = FakeState(), FakeNotifier()
    for allocated_gb in (ABOVE_CRITICAL_GB, ABOVE_WARNING_GB, BELOW_WARNING_GB, ABOVE_CRITICAL_GB):
        build_check(allocated_gb, state, notifier).run()
    assert len(notifier.sent) == 2


def test_a_failed_publish_does_not_save_the_level_so_the_next_run_retries() -> None:
    state = FakeState()
    with pytest.raises(RuntimeError):
        build_check(ABOVE_WARNING_GB, state, FakeNotifier(fail=True)).run()
    assert state.saved == []


def test_the_fraction_is_published_as_a_metric_every_run() -> None:
    metrics = FakeMetrics()
    build_check(BELOW_WARNING_GB, FakeState(), metrics=metrics).run()
    assert metrics.points == [(REGION, pytest.approx(BELOW_WARNING_GB / QUOTA_GB))]


def test_without_a_known_quota_nothing_is_published_or_saved() -> None:
    state, notifier, metrics = FakeState(), FakeNotifier(), FakeMetrics()
    check = QuotaAlertCheck(
        config=AlertConfig.from_environ(ENVIRON),
        quotas=FakeQuotas(report_without_quota()),
        state=state,
        notifier=notifier,
        metrics=metrics,
    )
    result = check.run()
    assert (result.level, result.fraction, result.notified) == ("ok", None, False)
    assert not state.saved and not notifier.sent and not metrics.points


def test_unresolved_sandboxes_are_mentioned_in_the_alert() -> None:
    notifier = FakeNotifier()
    build_check(ABOVE_WARNING_GB, FakeState(), notifier, unresolved=2).run()
    assert "No se pudo leer la memoria de 2 sandboxes" in notifier.sent[0][1]


def test_dynamo_state_store_reads_and_writes_one_row() -> None:
    client = FakeClient({"get_item": {"Item": {"level": {"S": "warning"}}}})
    store = DynamoStateStore(client, "table")
    assert store.last_level() == "warning"
    store.save_level("critical")
    (_, get), (_, put) = client.calls
    assert get["TableName"] == "table" and get["ConsistentRead"] is True
    assert put["Item"]["level"] == {"S": "critical"}


def test_dynamo_state_store_without_a_row_is_ok() -> None:
    assert DynamoStateStore(FakeClient({"get_item": {}}), "table").last_level() == "ok"


def test_sns_and_cloudwatch_adapters_send_the_expected_calls() -> None:
    sns, cloudwatch = FakeClient(), FakeClient()
    SnsNotifier(sns, TOPIC).publish("asunto", "mensaje")
    CloudWatchMetricSink(cloudwatch).put_fraction(REGION, 0.5)
    assert sns.calls == [
        ("publish", {"TopicArn": TOPIC, "Subject": "asunto", "Message": "mensaje"})
    ]
    ((_, put),) = cloudwatch.calls
    assert put["Namespace"] == QUOTA_METRIC_NAMESPACE
    assert put["MetricData"][0]["MetricName"] == QUOTA_METRIC_NAME
    assert put["MetricData"][0]["Dimensions"] == [{"Name": "Region", "Value": REGION}]


def test_config_defaults_to_the_named_thresholds() -> None:
    config = AlertConfig.from_environ(ENVIRON)
    assert config.warning_fraction == QUOTA_WARNING_FRACTION
    assert config.critical_fraction == QUOTA_CRITICAL_FRACTION


def test_config_reads_percent_overrides() -> None:
    config = AlertConfig.from_environ(
        {
            **ENVIRON,
            "RAYITO_ALERTS_WARNING_PERCENT": "50",
            "RAYITO_ALERTS_CRITICAL_PERCENT": "70",
        }
    )
    assert (config.warning_fraction, config.critical_fraction) == (0.5, 0.7)


@pytest.mark.parametrize(
    "override",
    [
        {"RAYITO_ALERTS_WARNING_PERCENT": "90", "RAYITO_ALERTS_CRITICAL_PERCENT": "80"},
        {"RAYITO_ALERTS_WARNING_PERCENT": "0"},
        {"RAYITO_ALERTS_CRITICAL_PERCENT": "101"},
        {"RAYITO_ALERTS_WARNING_PERCENT": "abc"},
    ],
)
def test_config_rejects_incoherent_thresholds(override: dict[str, str]) -> None:
    with pytest.raises(ConfigError):
        AlertConfig.from_environ({**ENVIRON, **override})


def test_config_requires_the_stack_variables() -> None:
    with pytest.raises(ConfigError):
        AlertConfig.from_environ({"AWS_REGION": REGION})
