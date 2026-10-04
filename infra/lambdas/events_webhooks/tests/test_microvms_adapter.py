"""`adapters.microvms.ListMicrovmsLister.running_sandbox_ids`: only
non-terminal `state`s count as live — regression for a `TERMINATED` (or
`TERMINATING`) microVM that `ListMicrovms` still lists staying "open"
forever, so the reconciler never synthesizes its `killed` event.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

import boto3
import pytest
from adapters.microvms import (
    SERVICE_NAME,
    ListMicrovmsLister,
    client_from_bundled_model,
)

REPO_ROOT = Path(__file__).resolve().parents[4]


class _FakeListMicrovmsClient:
    def __init__(self, pages: list[dict[str, Any]]) -> None:
        self._pages = pages
        self.calls: list[dict[str, Any]] = []

    def list_microvms(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        return self._pages[len(self.calls) - 1]


def _item(microvm_id: str, state: str) -> dict[str, str]:
    return {"microvmId": microvm_id, "state": state}


def test_only_non_terminal_states_count_as_live() -> None:
    client = _FakeListMicrovmsClient(
        [
            {
                "items": [
                    _item("sbx-pending", "PENDING"),
                    _item("sbx-running", "RUNNING"),
                    _item("sbx-suspending", "SUSPENDING"),
                    _item("sbx-suspended", "SUSPENDED"),
                    _item("sbx-terminating", "TERMINATING"),
                    _item("sbx-terminated", "TERMINATED"),
                ]
            }
        ]
    )
    lister = ListMicrovmsLister(client)
    assert lister.running_sandbox_ids() == {
        "sbx-pending",
        "sbx-running",
        "sbx-suspending",
        "sbx-suspended",
    }


def test_paginates_with_next_token() -> None:
    client = _FakeListMicrovmsClient(
        [
            {"items": [_item("sbx-1", "RUNNING")], "nextToken": "page-2"},
            {"items": [_item("sbx-2", "RUNNING")]},
        ]
    )
    lister = ListMicrovmsLister(client)
    assert lister.running_sandbox_ids() == {"sbx-1", "sbx-2"}
    assert client.calls == [{}, {"nextToken": "page-2"}]


def test_the_client_is_built_from_the_bundled_model_with_a_fresh_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The zip's layout (`scripts/gen_stack_assets.py`), from the model's
    # single source. A default session created first (as the reconciler's
    # DynamoDB resource does) must not hide it: the regression was a
    # `data_path` read once by that earlier session's loader.
    monkeypatch.delenv("AWS_DATA_PATH", raising=False)
    model = REPO_ROOT / "docs" / "aws-api" / "service-2.json"
    api_version = json.loads(model.read_bytes())["metadata"]["apiVersion"]
    target = tmp_path / "models" / SERVICE_NAME / api_version / "service-2.json"
    target.parent.mkdir(parents=True)
    shutil.copyfile(model, target)
    monkeypatch.setattr(boto3, "DEFAULT_SESSION", None)  # restored after the test
    boto3.setup_default_session(region_name="us-east-1")
    boto3.resource("dynamodb", region_name="us-east-1")

    client = client_from_bundled_model(tmp_path / "models", region_name="us-east-1")

    assert client.meta.service_model.service_name == SERVICE_NAME
    assert hasattr(client, "list_microvms")
