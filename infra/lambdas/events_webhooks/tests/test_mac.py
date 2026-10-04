from __future__ import annotations

import json
from pathlib import Path

from domain.event import LifecycleEvent, MalformedEventLine, parse_event_line
from domain.mac import derive_sandbox_key, verify_mac

REPO_ROOT = Path(__file__).resolve().parents[4]
VECTORS = json.loads(
    (REPO_ROOT / "testdata" / "lifecycle-events" / "mac-vectors.json").read_text(encoding="utf-8")
)


def test_derive_sandbox_key_matches_the_shared_vectors() -> None:
    for case in VECTORS["key_derivation"]:
        stack_key = bytes.fromhex(case["stack_key_hex"])
        derived = derive_sandbox_key(stack_key, case["sandbox_id"])
        assert derived.hex() == case["expected_k_sbx_hex"]


def test_verify_mac_accepts_the_shared_vectors_and_rejects_tampering() -> None:
    for case in VECTORS["event_mac"]:
        key = bytes.fromhex(case["k_sbx_hex"])
        payload = case["event_json"].encode("utf-8")
        mac = bytes.fromhex(case["expected_mac_hex"])
        assert verify_mac(key, payload, mac)
        assert not verify_mac(key, payload + b" ", mac)
        assert not verify_mac(key, payload, bytes(len(mac)))


def test_parse_event_line_round_trips_the_shared_vectors() -> None:
    for case in VECTORS["event_mac"]:
        line = case["expected_line"]
        payload, mac = parse_event_line(line)
        assert payload == case["event_json"].encode("utf-8")
        assert mac.hex() == case["expected_mac_hex"]


def test_parse_event_line_rejects_a_forged_prefix() -> None:
    try:
        parse_event_line("not.the.right.token a b")
    except MalformedEventLine:
        return
    raise AssertionError("expected MalformedEventLine")


def test_parse_event_line_rejects_invalid_base64() -> None:
    try:
        parse_event_line("rayito.event.v1 not-base64!! also-not")
    except MalformedEventLine:
        return
    raise AssertionError("expected MalformedEventLine")


def test_lifecycle_event_round_trips_through_json() -> None:
    event = LifecycleEvent(
        event_id="evt-1",
        sandbox_id="sbx-1",
        kind="killed",
        generation=2,
        occurred_at_ms=42,
        image_arn="arn:test",
        image_version="1",
        kill_reason="request",
    )
    payload = json.dumps(
        {
            "event_id": event.event_id,
            "sandbox_id": event.sandbox_id,
            "kind": event.kind,
            "generation": event.generation,
            "occurred_at_ms": event.occurred_at_ms,
            "image_arn": event.image_arn,
            "image_version": event.image_version,
            "kill_reason": event.kill_reason,
        }
    ).encode("utf-8")
    parsed = LifecycleEvent.from_json_bytes(payload)
    assert parsed == event
