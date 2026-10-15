"""Los adaptadores de AWS de los puertos: DynamoDB, SNS y CloudWatch. Cada
uno recibe su cliente de `boto3` ya construido."""

from __future__ import annotations

import time
from typing import Any, Final

from quota_alert_check.decision import Level, parse_level

from rayito._limits import QUOTA_METRIC_NAME, QUOTA_METRIC_NAMESPACE

STATE_KEY: Final = "memory"
KEY_ATTRIBUTE: Final = "check"
LEVEL_ATTRIBUTE: Final = "level"
UPDATED_ATTRIBUTE: Final = "updatedAt"
REGION_DIMENSION: Final = "Region"
FRACTION_UNIT: Final = "None"


class DynamoStateStore:
    def __init__(self, client: Any, table: str) -> None:
        self._client = client
        self._table = table

    def last_level(self) -> Level:
        response = self._client.get_item(
            TableName=self._table,
            Key={KEY_ATTRIBUTE: {"S": STATE_KEY}},
            ConsistentRead=True,
        )
        item = response.get("Item") or {}
        return parse_level(item.get(LEVEL_ATTRIBUTE, {}).get("S"))

    def save_level(self, level: Level) -> None:
        self._client.put_item(
            TableName=self._table,
            Item={
                KEY_ATTRIBUTE: {"S": STATE_KEY},
                LEVEL_ATTRIBUTE: {"S": level},
                UPDATED_ATTRIBUTE: {"N": str(int(time.time()))},
            },
        )


class SnsNotifier:
    def __init__(self, client: Any, topic_arn: str) -> None:
        self._client = client
        self._topic_arn = topic_arn

    def publish(self, subject: str, message: str) -> None:
        self._client.publish(TopicArn=self._topic_arn, Subject=subject, Message=message)


class CloudWatchMetricSink:
    def __init__(self, client: Any) -> None:
        self._client = client

    def put_fraction(self, region: str, fraction: float) -> None:
        self._client.put_metric_data(
            Namespace=QUOTA_METRIC_NAMESPACE,
            MetricData=[
                {
                    "MetricName": QUOTA_METRIC_NAME,
                    "Dimensions": [{"Name": REGION_DIMENSION, "Value": region}],
                    "Value": fraction,
                    "Unit": FRACTION_UNIT,
                }
            ],
        )
