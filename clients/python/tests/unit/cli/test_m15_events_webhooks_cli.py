"""`rayito events deploy|status|destroy|list` and `webhook add|list|remove`
(m15-events-webhooks): the CLI only parses arguments and forwards them to
`LifecycleEvents`, which `_service.py`'s own unit tests already cover
against fakes — this file only checks the wiring.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest
from typer.testing import CliRunner

from rayito.cli import events as events_cli
from rayito.cli.app import app


@dataclass
class FakeLifecycleEvents:
    calls: list[tuple[str, dict[str, Any]]] = field(default_factory=list)

    def deploy(self, **kwargs: Any) -> Any:
        self.calls.append(("deploy", kwargs))

        class _Status:
            name = "rayito-events-webhooks"
            state = "CREATE_COMPLETE"
            outputs: dict[str, str] = {}

        return _Status()

    def status(self) -> None:
        self.calls.append(("status", {}))
        return None

    def destroy(self, **kwargs: Any) -> None:
        self.calls.append(("destroy", kwargs))

    def register_webhook(self, url: str, **kwargs: Any) -> Any:
        self.calls.append(("register_webhook", {"url": url, **kwargs}))

        class _Webhook:
            webhook_id = "abc123"

        w = _Webhook()
        w.url = url  # type: ignore[attr-defined]
        w.types = tuple(kwargs["types"])  # type: ignore[attr-defined]
        return w

    def list_webhooks(self) -> list[Any]:
        self.calls.append(("list_webhooks", {}))
        return []

    def delete_webhook(self, webhook_id: str) -> None:
        self.calls.append(("delete_webhook", {"webhook_id": webhook_id}))

    def get_events(self, **kwargs: Any) -> list[Any]:
        self.calls.append(("get_events", kwargs))
        return []


@pytest.fixture
def fake_events(monkeypatch: pytest.MonkeyPatch) -> FakeLifecycleEvents:
    fake = FakeLifecycleEvents()

    def factory(ctx: Any, stack_name: str) -> FakeLifecycleEvents:
        assert isinstance(stack_name, str)
        return fake

    monkeypatch.setattr(events_cli, "_events", factory)
    return fake


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


def test_deploy_forwards_its_arguments(runner: CliRunner, fake_events: FakeLifecycleEvents) -> None:
    result = runner.invoke(
        app,
        [
            "events",
            "deploy",
            "--artifact-bucket",
            "mi-bucket",
            "--log-group-name",
            "/rayito/rayito-base",
            "--yes",
        ],
    )
    assert result.exit_code == 0, result.output
    assert fake_events.calls[0][0] == "deploy"
    assert fake_events.calls[0][1]["artifact_bucket"] == "mi-bucket"
    assert fake_events.calls[0][1]["log_group_name"] == "/rayito/rayito-base"


def test_deploy_without_yes_asks_for_confirmation(
    runner: CliRunner, fake_events: FakeLifecycleEvents
) -> None:
    result = runner.invoke(
        app,
        [
            "events",
            "deploy",
            "--artifact-bucket",
            "mi-bucket",
            "--log-group-name",
            "/rayito/rayito-base",
        ],
        input="n\n",
    )
    assert result.exit_code != 0
    assert fake_events.calls == []


def test_webhook_add_forwards_url_secret_and_types(
    runner: CliRunner, fake_events: FakeLifecycleEvents
) -> None:
    result = runner.invoke(
        app,
        [
            "events",
            "webhook",
            "add",
            "https://hooks.example.com",
            "--secret-name",
            "mi-webhook",
            "--type",
            "sandbox.lifecycle.killed",
        ],
    )
    assert result.exit_code == 0, result.output
    call = fake_events.calls[0]
    assert call == (
        "register_webhook",
        {
            "url": "https://hooks.example.com",
            "secret_name": "mi-webhook",
            "types": ["sandbox.lifecycle.killed"],
        },
    )


def test_webhook_remove_forwards_the_id(
    runner: CliRunner, fake_events: FakeLifecycleEvents
) -> None:
    result = runner.invoke(app, ["events", "webhook", "remove", "abc123"])
    assert result.exit_code == 0, result.output
    assert fake_events.calls == [("delete_webhook", {"webhook_id": "abc123"})]


def test_list_forwards_filters(runner: CliRunner, fake_events: FakeLifecycleEvents) -> None:
    result = runner.invoke(
        app, ["events", "list", "--sandbox-id", "sbx-1", "--type", "sandbox.lifecycle.killed"]
    )
    assert result.exit_code == 0, result.output
    assert fake_events.calls[0][0] == "get_events"
    assert fake_events.calls[0][1]["sandbox_id"] == "sbx-1"
    assert fake_events.calls[0][1]["types"] == ["sandbox.lifecycle.killed"]
