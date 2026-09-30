"""Secrets Manager falso para los tests de M13a.

- `FakeSecretsManager`: un cliente con la forma de botocore
  (`get_secret_value(**params) -> dict`, `ClientError` con `Code`) que
  guarda secretos en memoria, cuenta llamadas por operación y puede
  retrasar `GetSecretValue` para forzar concurrencia real.
- `SpySession`: una sesión boto3 falsa que devuelve ese cliente y apunta
  cada `client(servicio)` construido (la "sesión espía").
- `stubbed_client()`: un cliente `secretsmanager` real de botocore con
  `Stubber`, para comprobar los parámetros contra el modelo del servicio.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

SENTINEL_VALUE = "sk-SENTINEL-4f1c9e0d-never-log-me"
SENTINEL_NAME = "confidential-selector-name"
REGION = "us-east-1"
ACCOUNT = "000000000000"


def arn_for(secret_id: str) -> str:
    return f"arn:aws:secretsmanager:{REGION}:{ACCOUNT}:secret:{secret_id}-AbCdEf"


def client_error(code: str, message: str, operation: str) -> ClientError:
    return ClientError(
        {"Error": {"Code": code, "Message": message}, "ResponseMetadata": {"HTTPStatusCode": 400}},
        operation,
    )


@dataclass
class StoredSecret:
    arn: str
    name: str
    description: str
    versions: dict[str, str] = field(default_factory=dict)
    current: str = ""
    created: datetime = field(default_factory=lambda: datetime.now(UTC))


@dataclass(eq=False)
class FakeSecretsManager:
    """Semántica mínima de Secrets Manager para la caché y la inyección."""

    get_delay: float = 0.0
    secrets: dict[str, StoredSecret] = field(default_factory=dict)
    calls: dict[str, int] = field(default_factory=dict)
    requests: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    lock: threading.Lock = field(default_factory=threading.Lock)

    def count(self, operation: str) -> int:
        with self.lock:
            return self.calls.get(operation, 0)

    def put(self, secret_id: str, value: str, *, version_id: str = "v1") -> None:
        stored = self.secrets.get(secret_id) or StoredSecret(
            arn=arn_for(secret_id), name=secret_id, description=""
        )
        stored.versions[version_id] = value
        stored.current = version_id
        self.secrets[secret_id] = stored

    def _record(self, operation: str, params: dict[str, Any]) -> None:
        with self.lock:
            self.calls[operation] = self.calls.get(operation, 0) + 1
            self.requests.append((operation, dict(params)))

    def _find(self, secret_id: str, operation: str) -> StoredSecret:
        for stored in self.secrets.values():
            if secret_id in (stored.name, stored.arn):
                return stored
        raise client_error(
            "ResourceNotFoundException",
            "Secrets Manager can't find the specified secret.",
            operation,
        )

    def get_secret_value(self, **params: Any) -> dict[str, Any]:
        self._record("GetSecretValue", params)
        if self.get_delay:
            time.sleep(self.get_delay)
        stored = self._find(params["SecretId"], "GetSecretValue")
        version = params.get("VersionId") or stored.current
        if version not in stored.versions:
            raise client_error("ResourceNotFoundException", "no version", "GetSecretValue")
        return {
            "ARN": stored.arn,
            "Name": stored.name,
            "VersionId": version,
            "SecretString": stored.versions[version],
            "CreatedDate": stored.created,
        }

    def describe_secret(self, **params: Any) -> dict[str, Any]:
        self._record("DescribeSecret", params)
        stored = self._find(params["SecretId"], "DescribeSecret")
        return {
            "ARN": stored.arn,
            "Name": stored.name,
            "Description": stored.description,
            "CreatedDate": stored.created,
            "LastChangedDate": stored.created,
            "VersionIdsToStages": {
                version: (["AWSCURRENT"] if version == stored.current else ["AWSPREVIOUS"])
                for version in stored.versions
            },
        }

    def create_secret(self, **params: Any) -> dict[str, Any]:
        self._record("CreateSecret", params)
        name = params["Name"]
        if name in self.secrets:
            raise client_error("ResourceExistsException", f"{name} already exists", "CreateSecret")
        stored = StoredSecret(
            arn=arn_for(name), name=name, description=params.get("Description", "")
        )
        token = params["ClientRequestToken"]
        stored.versions[token] = params["SecretString"]
        stored.current = token
        self.secrets[name] = stored
        return {"ARN": stored.arn, "Name": name, "VersionId": token}

    def put_secret_value(self, **params: Any) -> dict[str, Any]:
        self._record("PutSecretValue", params)
        stored = self._find(params["SecretId"], "PutSecretValue")
        token = params["ClientRequestToken"]
        stored.versions[token] = params["SecretString"]
        stored.current = token
        return {"ARN": stored.arn, "Name": stored.name, "VersionId": token}

    def update_secret(self, **params: Any) -> dict[str, Any]:
        self._record("UpdateSecret", params)
        stored = self._find(params["SecretId"], "UpdateSecret")
        stored.description = params["Description"]
        return {"ARN": stored.arn, "Name": stored.name}

    def list_secrets(self, **params: Any) -> dict[str, Any]:
        self._record("ListSecrets", params)
        prefixes = [
            value
            for flt in params.get("Filters", [])
            if flt["Key"] == "name"
            for value in flt["Values"]
        ]
        entries = [
            {
                "ARN": stored.arn,
                "Name": stored.name,
                "Description": stored.description,
                "CreatedDate": stored.created,
                "LastChangedDate": stored.created,
                "SecretVersionsToStages": {stored.current: ["AWSCURRENT"]},
            }
            for stored in sorted(self.secrets.values(), key=lambda item: item.name)
            if not prefixes or any(stored.name.startswith(prefix) for prefix in prefixes)
        ]
        start = int(params.get("NextToken", "0"))
        size = int(params.get("MaxResults", 100))
        page = entries[start : start + size]
        response: dict[str, Any] = {"SecretList": page}
        if start + size < len(entries):
            response["NextToken"] = str(start + size)
        return response

    def delete_secret(self, **params: Any) -> dict[str, Any]:
        self._record("DeleteSecret", params)
        stored = self._find(params["SecretId"], "DeleteSecret")
        del self.secrets[stored.name]
        return {"ARN": stored.arn, "Name": stored.name}


@dataclass(eq=False)
class SpySession:
    """Sesión boto3 falsa: `client("secretsmanager")` devuelve `api` y queda
    apuntado; cualquier otro servicio es un error del test."""

    api: FakeSecretsManager = field(default_factory=FakeSecretsManager)
    region_name: str | None = REGION
    built: list[str] = field(default_factory=list)

    def client(self, service: str, **kwargs: Any) -> Any:
        self.built.append(service)
        assert service == "secretsmanager", service
        return self.api


def stubbed_client() -> Any:
    session = boto3.session.Session(
        region_name=REGION, aws_access_key_id="testing", aws_secret_access_key="testing"
    )
    return session.client(
        "secretsmanager", config=Config(retries={"total_max_attempts": 1, "mode": "standard"})
    )
