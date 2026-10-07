"""Dominio puro de `m15-events-webhooks`: derivación de `k_sbx` contra los
vectores compartidos con Rust y TypeScript, y los tipos de evento."""

from __future__ import annotations

import json
from pathlib import Path

from rayito._lifecycle_events._domain import EventRecord, event_type
from rayito._lifecycle_events._keys import derive_sandbox_key

REPO_ROOT = Path(__file__).resolve().parents[4]
VECTORS = REPO_ROOT / "testdata" / "lifecycle-events" / "mac-vectors.json"


def test_derive_sandbox_key_matches_the_shared_vectors() -> None:
    data = json.loads(VECTORS.read_text(encoding="utf-8"))
    cases = data["key_derivation"]
    assert cases, "el fichero de vectores no debe estar vacío"
    for case in cases:
        stack_key = bytes.fromhex(case["stack_key_hex"])
        derived = derive_sandbox_key(stack_key, case["sandbox_id"])
        assert derived.hex() == case["expected_k_sbx_hex"]


def test_a_different_sandbox_id_never_produces_the_same_key() -> None:
    stack_key = b"a-stack-secret"
    key_a = derive_sandbox_key(stack_key, "sbx-a")
    key_b = derive_sandbox_key(stack_key, "sbx-b")
    assert key_a != key_b


def test_event_type_and_execution_id() -> None:
    record = EventRecord(
        event_id="evt-1",
        sandbox_id="sbx-1",
        kind="killed",
        kill_reason="request",
        generation=3,
        occurred_at_ms=42,
        image_arn="arn:test",
        image_version="1",
    )
    assert record.type == "sandbox.lifecycle.killed"
    assert record.sandbox_execution_id == "sbx-1#3"
    assert event_type("created") == "sandbox.lifecycle.created"


URL_VECTORS = REPO_ROOT / "testdata" / "lifecycle-events" / "webhook-url-vectors.json"


def test_webhook_url_check_matches_the_shared_vectors() -> None:
    from rayito._lifecycle_events._domain import is_deliverable_webhook_url

    data = json.loads(URL_VECTORS.read_text(encoding="utf-8"))
    for url in data["accepted"]:
        assert is_deliverable_webhook_url(url), url
    for url in data["rejected"]:
        assert not is_deliverable_webhook_url(url), url


SSRF_VECTORS = REPO_ROOT / "testdata" / "lifecycle-events" / "ssrf-address-vectors.json"


def _url_for(address: str) -> str:
    host = f"[{address}]" if ":" in address else address
    return f"https://{host}/hook"


def test_literal_addresses_get_the_deliverer_ssrf_verdict() -> None:
    """Los mismos vectores que `infra/lambdas/events_webhooks/tests/test_ssrf.py`:
    una IP que el deliverer bloquearía no se registra, y una pública sí."""
    import ipaddress

    from rayito._lifecycle_events._domain import (
        is_blocked_webhook_address,
        is_deliverable_webhook_url,
    )

    data = json.loads(SSRF_VECTORS.read_text(encoding="utf-8"))
    assert data["blocked"] and data["allowed"]
    for address in data["blocked"]:
        assert is_blocked_webhook_address(ipaddress.ip_address(address)), address
        assert not is_deliverable_webhook_url(_url_for(address)), address
    for address in data["allowed"]:
        assert not is_blocked_webhook_address(ipaddress.ip_address(address)), address
        assert is_deliverable_webhook_url(_url_for(address)), address
