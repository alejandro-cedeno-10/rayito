"""El punto de entrada de la función (`Handler: handler.lambda_handler` en
`infra/quota-alerts.yaml`). Construye la comprobación una vez por entorno de
ejecución y la corre en cada invocación del `rate(...)` de EventBridge."""

from __future__ import annotations

import os
from functools import cache
from typing import Any

import boto3
from quota_alert_check.adapters import (
    CloudWatchMetricSink,
    DynamoStateStore,
    SnsNotifier,
)
from quota_alert_check.config import AlertConfig
from quota_alert_check.runner import QuotaAlertCheck

from rayito._aws import LambdaMicrovmsControlPlane
from rayito._dashboard._quotas import Quotas


@cache
def check() -> QuotaAlertCheck:
    config = AlertConfig.from_environ(os.environ)
    session = boto3.session.Session(region_name=config.region)
    plane = LambdaMicrovmsControlPlane.from_session(session, region=config.region)
    return QuotaAlertCheck(
        config=config,
        quotas=Quotas(region=config.region, session=session, control_plane=plane),
        state=DynamoStateStore(session.client("dynamodb"), config.state_table),
        notifier=SnsNotifier(session.client("sns"), config.topic_arn),
        metrics=CloudWatchMetricSink(session.client("cloudwatch")),
    )


def lambda_handler(event: dict[str, Any], context: object) -> dict[str, Any]:
    result = check().run()
    return {"level": result.level, "notified": result.notified}
