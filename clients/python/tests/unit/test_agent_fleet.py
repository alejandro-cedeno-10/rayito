"""`AgentFleet` y `AsyncAgentFleet` (`_fleet.py`, `_fleet_async.py`) sobre
dobles: el arranque todo o nada, el id de sesión enrutable, la cola cliente y
el 429, las sesiones dedicadas y su índice, la salud y la sustitución, el
escalado y la vida de los VMs sobrantes, las plazas calientes, la
reconexión y el cierre. Ninguna prueba llama a AWS."""

from __future__ import annotations

import logging
import math
from typing import Any

import pytest

from rayito._agent._events import Done, Text
from rayito._agent._fleet import AgentFleet
from rayito._agent._fleet_async import AsyncAgentFleet
from rayito._agent._fleet_base import (
    AGENT_FLEET_BUSY_RETRY_SECONDS,
    AGENT_FLEET_SESSION_LABEL,
    UNMANAGED_LIFETIME_MESSAGE,
    session_label,
)
from rayito._agent._project_base import request_timeout
from rayito._agent._scaling_domain import ScalingOptions, decode_handle, encode_handle
from rayito._limits import AGENT_FLEET_UNHEALTHY_PINGS, AGENT_SERVICE_QUEUE_TIMEOUT_SECONDS_DEFAULT
from rayito.exceptions import (
    CapacityException,
    InvalidArgumentException,
    SandboxException,
)
from tests.unit.fake_agent_fleet import FLEET_TOKEN as TOKEN
from tests.unit.fake_agent_fleet import FleetFlavor, sandbox_id_of
from tests.unit.fake_agent_project import project

SHARED_TWO = ScalingOptions(min_vms=1, max_vms=2)
DEDICATED_TWO = ScalingOptions(min_vms=1, max_vms=2, sessions="dedicated")


@pytest.fixture(params=["sync", "async"])
def flavor(request: pytest.FixtureRequest) -> FleetFlavor:
    return FleetFlavor(request.param)


def host_of(flav: FleetFlavor, number: int) -> Any:
    return flav.backend.host(sandbox_id_of(number))


# ---------------------------------------------------------------------- start


def test_start_creates_min_vms_with_the_fleet_token_and_the_deploy_arguments(
    flavor: FleetFlavor,
) -> None:
    fleet = flavor.started(ScalingOptions(min_vms=2, max_vms=3))

    assert len(fleet.sandbox_ids) == 2
    assert fleet.access_token == TOKEN
    for template, arguments in flavor.classes.created:
        assert template == "support-bot"
        assert arguments["access_token"] == TOKEN
        assert arguments["metadata"]["rayito.agent.deployment"] == "support-bot"
    assert all(sbx.served for sbx in flavor.classes.live)


def test_a_failed_start_terminates_what_it_created(flavor: FleetFlavor) -> None:
    fleet = flavor.fleet(ScalingOptions(min_vms=2, max_vms=2))
    flavor.classes.add()
    calls = {"count": 0}
    original = flavor.classes._create

    def flaky(template: str, kwargs: dict[str, Any]) -> Any:
        calls["count"] += 1
        if calls["count"] == 2:
            raise SandboxException("sin capacidad")
        return original(template, kwargs)

    flavor.classes._create = flaky  # type: ignore[method-assign]
    fleet._members.clear()

    with pytest.raises(SandboxException):
        flavor.call(fleet.start())

    created = [sbx for sbx in flavor.classes.sandboxes.values() if sbx.number > 1]
    assert created
    assert all(sbx.state == "TERMINATED" for sbx in created)


def test_a_fleet_without_a_project_cannot_start(flavor: FleetFlavor) -> None:
    fleet = (AsyncAgentFleet if flavor.asynchronous else AgentFleet)(
        None, name="support-bot", access_token=TOKEN, directory=flavor.classes_directory()
    )

    with pytest.raises(InvalidArgumentException):
        flavor.call(fleet.start())


# ------------------------------------------------------------------- sessions


def test_a_single_vm_fleet_keeps_the_session_id_unchanged(flavor: FleetFlavor) -> None:
    fleet = flavor.started()

    result = flavor.call(fleet.invoke("hola"))

    assert not result.session_id.startswith("rs1.")
    assert flavor.call(fleet.invoke("otra", session_id=result.session_id)).session_id == (
        result.session_id
    )


def test_a_routable_fleet_returns_a_handle_that_routes_back_to_the_same_vm(
    flavor: FleetFlavor,
) -> None:
    fleet = flavor.started(SHARED_TWO)

    first = flavor.call(fleet.invoke("hola"))
    route = decode_handle(first.session_id)
    second = flavor.call(fleet.invoke("sigue", session_id=first.session_id))

    assert route is not None
    assert route.sandbox_id == fleet.sandbox_ids[0]
    assert second.session_id == first.session_id
    sent = host_of(flavor, 1).invocations
    assert [item.get("session_id") for item in sent] == [None, route.session_id]


def test_an_unknown_handle_is_a_lost_vm(flavor: FleetFlavor) -> None:
    fleet = flavor.started(SHARED_TWO)
    ghost = encode_handle(sandbox_id_of(99), "ses_x")

    with pytest.raises(SandboxException) as raised:
        flavor.call(fleet.invoke("hola", session_id=ghost))

    assert raised.value.code == "agent_vm_lost"


def test_the_handle_of_a_vm_that_appeared_later_is_found_by_listing(flavor: FleetFlavor) -> None:
    fleet = flavor.started(SHARED_TWO)
    later = flavor.classes.add()

    result = flavor.call(
        fleet.invoke("hola", session_id=encode_handle(later.sandbox_id, "ses_later"))
    )

    assert decode_handle(result.session_id).sandbox_id == later.sandbox_id  # type: ignore[union-attr]
    assert later.sandbox_id in fleet.sandbox_ids


# ---------------------------------------------------------------- shared mode


def test_a_second_concurrent_session_grows_the_fleet_and_limits_the_extra_vm(
    flavor: FleetFlavor,
) -> None:
    scaling = ScalingOptions(min_vms=1, max_vms=2, scale_in_after_seconds=120)
    fleet = flavor.started(scaling)
    held = flavor.open_stream(fleet, "primera")

    second = flavor.call(fleet.invoke("segunda"))
    flavor.close_stream(held)

    assert len(fleet.sandbox_ids) == 2
    assert decode_handle(second.session_id).sandbox_id == fleet.sandbox_ids[1]  # type: ignore[union-attr]
    extra = flavor.classes.sandboxes[fleet.sandbox_ids[1]]
    baseline = flavor.classes.sandboxes[fleet.sandbox_ids[0]]
    hold = math.ceil(request_timeout(project()))
    assert extra.timeouts == [120, hold, 120]
    assert baseline.timeouts == []


def test_a_fleet_that_can_grow_creates_every_vm_with_a_logical_lifetime(
    flavor: FleetFlavor,
) -> None:
    fleet = flavor.started(SHARED_TWO)
    held = flavor.open_stream(fleet, "primera")
    flavor.call(fleet.invoke("segunda"))
    flavor.close_stream(held)

    assert len(flavor.classes.created) == 2
    for _template, arguments in flavor.classes.created:
        assert arguments["timeout"] == 3600
        assert arguments["max_lifetime"] == 3660
        assert arguments["on_timeout"] == "kill"
    assert all(sbx.managed for sbx in flavor.classes.live)


def test_a_single_vm_fleet_creates_its_vm_as_in_0_16(flavor: FleetFlavor) -> None:
    flavor.started()

    [(_template, arguments)] = flavor.classes.created
    assert "max_lifetime" not in arguments
    assert "on_timeout" not in arguments


def test_a_vm_without_a_logical_lifetime_warns_once_and_is_not_retried(
    flavor: FleetFlavor, caplog: pytest.LogCaptureFixture
) -> None:
    fleet = flavor.started(SHARED_TWO)
    old = flavor.classes.add(managed=False)
    flavor.call(fleet.refresh_members())
    held = flavor.open_stream(fleet, "primera")

    with caplog.at_level(logging.WARNING, logger="rayito"):
        flavor.call(fleet.invoke("segunda"))
        flavor.call(fleet.invoke("tercera"))
    flavor.close_stream(held)

    assert old.timeouts == []
    assert [r.getMessage() for r in caplog.records].count(UNMANAGED_LIFETIME_MESSAGE) == 1


def test_a_vm_promoted_to_base_gets_its_whole_lifetime_back(flavor: FleetFlavor) -> None:
    fleet = flavor.started(SHARED_TWO)
    held = flavor.open_stream(fleet, "primera")
    flavor.call(fleet.invoke("segunda"))
    flavor.close_stream(held)
    base, extra_id = fleet.sandbox_ids
    extra = flavor.classes.sandboxes[extra_id]
    flavor.classes.sandboxes[base]._kill()
    flavor.call(fleet.refresh_members())

    flavor.call(fleet.invoke("tercera"))

    restored = extra.timeouts[-1]
    assert 3600 < restored <= 3655


def test_sessions_share_a_vm_up_to_its_concurrency(flavor: FleetFlavor) -> None:
    the_project = project(serve=_serve(max_concurrency=2))
    fleet = flavor.started(SHARED_TWO, the_project=the_project)
    held = flavor.open_stream(fleet, "primera")

    flavor.call(fleet.invoke("segunda"))
    flavor.close_stream(held)

    assert len(fleet.sandbox_ids) == 1


def test_a_full_fleet_queues_and_then_reports_capacity(flavor: FleetFlavor) -> None:
    fleet = flavor.started()
    held = flavor.open_stream(fleet, "primera")

    with pytest.raises(CapacityException) as raised:
        flavor.call(fleet.invoke("segunda"))

    flavor.close_stream(held)
    assert raised.value.code == "agent_capacity"
    assert sum(flavor.clock.sleeps) >= AGENT_SERVICE_QUEUE_TIMEOUT_SECONDS_DEFAULT
    assert set(flavor.clock.sleeps) == {AGENT_FLEET_BUSY_RETRY_SECONDS}


def test_a_queued_session_gets_the_vm_once_it_frees(flavor: FleetFlavor) -> None:
    fleet = flavor.started()
    held = flavor.open_stream(fleet, "primera")
    closed = {"done": False}
    original_sleep = fleet._sleep

    async def release_then_sleep_async(seconds: float) -> None:
        if not closed["done"]:
            closed["done"] = True
            await held.close()
        await original_sleep(seconds)

    def release_then_sleep(seconds: float) -> None:
        if not closed["done"]:
            closed["done"] = True
            held.close()
        original_sleep(seconds)

    fleet._sleep = release_then_sleep_async if flavor.asynchronous else release_then_sleep

    result = flavor.call(fleet.invoke("segunda"))

    assert result.text == "respuesta a segunda"
    assert len(flavor.clock.sleeps) == 1


def test_a_429_from_the_host_is_retried_until_it_answers(flavor: FleetFlavor) -> None:
    fleet = flavor.started()
    host_of(flavor, 1).busy_answers = 2

    result = flavor.call(fleet.invoke("hola"))

    assert result.text == "respuesta a hola"
    assert len(host_of(flavor, 1).invocations) == 3
    assert flavor.clock.sleeps == [AGENT_FLEET_BUSY_RETRY_SECONDS] * 2


def test_a_host_that_never_stops_answering_429_is_capacity(flavor: FleetFlavor) -> None:
    fleet = flavor.started()
    host_of(flavor, 1).busy_answers = 10_000

    with pytest.raises(CapacityException) as raised:
        flavor.call(fleet.invoke("hola"))

    assert raised.value.code == "agent_capacity"
    assert raised.value.__cause__ is None


def test_a_429_moves_a_new_session_to_another_vm(flavor: FleetFlavor) -> None:
    fleet = flavor.started(ScalingOptions(min_vms=2, max_vms=2))
    host_of(flavor, 1).busy_answers = 1

    result = flavor.call(fleet.invoke("hola"))

    route = decode_handle(result.session_id)
    assert route is not None
    assert route.sandbox_id == sandbox_id_of(2)


# ----------------------------------------------------------------- dedicated


def test_dedicated_sessions_each_get_their_own_vm(flavor: FleetFlavor) -> None:
    fleet = flavor.started(DEDICATED_TWO)

    first = flavor.call(fleet.invoke("uno"))
    second = flavor.call(fleet.invoke("dos"))

    routes = {decode_handle(r.session_id).sandbox_id for r in (first, second)}  # type: ignore[union-attr]
    assert len(routes) == 2


def test_dedicated_capacity_is_the_number_of_vms(flavor: FleetFlavor) -> None:
    fleet = flavor.started(DEDICATED_TWO)
    flavor.call(fleet.invoke("uno"))
    flavor.call(fleet.invoke("dos"))

    with pytest.raises(CapacityException):
        flavor.call(fleet.invoke("tres"))


def test_dedicated_keeps_a_caller_chosen_session_on_its_vm_within_the_process(
    flavor: FleetFlavor,
) -> None:
    fleet = flavor.started(DEDICATED_TWO)

    first = flavor.call(fleet.invoke("uno", session_id="chat-1"))
    again = flavor.call(fleet.invoke("dos", session_id="chat-1"))

    assert first.session_id == again.session_id
    assert len(fleet.sandbox_ids) == 1


def test_dedicated_with_an_index_labels_the_vm_and_finds_it_again(flavor: FleetFlavor) -> None:
    index = object()
    fleet = flavor.started(
        ScalingOptions(min_vms=1, max_vms=3, sessions="dedicated"), session_index=index
    )
    flavor.call(fleet.invoke("uno"))

    flavor.call(fleet.invoke("hola", session_id="chat-9"))

    label = session_label("chat-9")
    created = flavor.classes.created[-1][1]
    assert created["metadata"][AGENT_FLEET_SESSION_LABEL] == label
    assert created["index"] is index
    assert "chat-9" not in str(created["metadata"])
    flavor.classes.session_hits[label] = fleet.sandbox_ids[-1]
    fresh = flavor.fleet(
        ScalingOptions(min_vms=1, max_vms=3, sessions="dedicated"), session_index=index
    )
    flavor.call(fresh.refresh_members())
    routed = flavor.call(fresh.invoke("otra", session_id="chat-9"))
    assert decode_handle(routed.session_id).sandbox_id == fleet.sandbox_ids[-1]  # type: ignore[union-attr]


# --------------------------------------------------------------------- health


def test_a_vm_that_stops_answering_is_replaced_to_keep_min_vms(flavor: FleetFlavor) -> None:
    fleet = flavor.started()
    original = fleet.sandbox_ids[0]
    host_of(flavor, 1).ping_status = 503

    for _ in range(AGENT_FLEET_UNHEALTHY_PINGS):
        flavor.call(fleet.status())

    assert original not in fleet.sandbox_ids
    assert len(fleet.sandbox_ids) == 1
    assert flavor.classes.sandboxes[original].state == "TERMINATED"


def test_status_reports_members_without_opening_the_unknown_ones(flavor: FleetFlavor) -> None:
    flavor.classes.add()
    flavor.classes.add()
    fleet = flavor.call(
        (AsyncAgentFleet if flavor.asynchronous else AgentFleet).connect(
            "support-bot",
            access_token=TOKEN,
            directory=flavor.classes_directory(),
        )
    )

    status = flavor.call(fleet.status())

    assert [member.state for member in status.members] == ["unknown", "unknown"]
    assert flavor.classes.connected == []
    assert (status.healthy, status.busy) == (0, 0)


def test_a_transport_failure_is_not_enough_to_drop_the_vm(flavor: FleetFlavor) -> None:
    fleet = flavor.started()
    host_of(flavor, 1).invoke_status = 500

    with pytest.raises(SandboxException):
        flavor.call(fleet.invoke("hola"))

    assert fleet.sandbox_ids == (sandbox_id_of(1),)


# -------------------------------------------------------------------- scaling


def test_scale_up_creates_vms_and_scale_down_terminates_the_free_extras(
    flavor: FleetFlavor,
) -> None:
    fleet = flavor.started(ScalingOptions(min_vms=1, max_vms=3))

    flavor.call(fleet.scale(min_vms=3))
    assert len(fleet.sandbox_ids) == 3
    flavor.call(fleet.scale(min_vms=1, max_vms=1))

    assert fleet.sandbox_ids == (sandbox_id_of(1),)
    assert len(flavor.classes.live) == 1
    assert fleet.scaling == ScalingOptions(min_vms=1, max_vms=1)


def test_scale_validates_like_scaling_options(flavor: FleetFlavor) -> None:
    fleet = flavor.started()

    with pytest.raises(InvalidArgumentException):
        flavor.call(fleet.scale(min_vms=5, max_vms=2))


# ------------------------------------------------------------------ warm pool


def test_warm_slots_come_from_a_pool_with_the_fleet_token(flavor: FleetFlavor) -> None:
    scaling = ScalingOptions(min_vms=1, max_vms=3, warm_vms=1)
    fleet = flavor.started(scaling)

    pool = flavor.pools[0]
    assert pool.started
    assert pool.config.size == 1
    assert pool.config.access_token == TOKEN
    assert flavor.call(fleet.wait_warm(timeout=5, poll=0.1))
    held = flavor.open_stream(fleet, "primera")
    flavor.call(fleet.invoke("segunda"))
    flavor.close_stream(held)
    assert len(pool.taken) == 1
    assert "bedrock" in pool.taken[0]["gateways"]
    assert len(fleet.sandbox_ids) == 2
    assert pool.config.max_lifetime == 3660
    assert pool.config.on_timeout == "kill"
    taken = flavor.classes.sandboxes[fleet.sandbox_ids[1]]
    assert taken.timeouts[0] == scaling.scale_in_after_seconds


def test_the_parked_slots_are_not_members_until_claimed(flavor: FleetFlavor) -> None:
    fleet = flavor.started(ScalingOptions(min_vms=1, max_vms=3, warm_vms=1))

    flavor.call(fleet.refresh_members())

    assert len(fleet.sandbox_ids) == 1


def test_stop_drains_the_pool_and_terminates_every_vm(flavor: FleetFlavor) -> None:
    fleet = flavor.started(ScalingOptions(min_vms=2, max_vms=3, warm_vms=1))

    flavor.call(fleet.stop())

    assert flavor.pools[0].closed == [True]
    assert flavor.classes.live == []
    assert fleet.sandbox_ids == ()


def test_detach_keeps_the_vms_and_the_parked_slots_with_a_persistent_backend(
    flavor: FleetFlavor,
) -> None:
    fleet = flavor.started(ScalingOptions(min_vms=1, max_vms=2, warm_vms=1), pool_backend=object())

    flavor.call(fleet.detach())

    assert flavor.pools[0].closed == [False]
    assert len(flavor.classes.live) == 2


def test_detach_without_a_persistent_backend_drains_the_pool(flavor: FleetFlavor) -> None:
    fleet = flavor.started(ScalingOptions(min_vms=1, max_vms=2, warm_vms=1))

    flavor.call(fleet.detach())

    assert flavor.pools[0].closed == [True]


# ------------------------------------------------------------------ reconnect


def test_connect_lists_the_image_and_opens_a_vm_only_when_it_is_used(flavor: FleetFlavor) -> None:
    flavor.classes.add()
    flavor.classes.add()
    fleet = flavor.call(
        (AsyncAgentFleet if flavor.asynchronous else AgentFleet).connect(
            "support-bot",
            access_token=TOKEN,
            scaling=SHARED_TWO,
            directory=flavor.classes_directory(),
            transport_factory=flavor.backend.async_factory
            if flavor.asynchronous
            else flavor.backend.factory,
        )
    )

    assert fleet.sandbox_ids == (sandbox_id_of(1), sandbox_id_of(2))
    assert flavor.classes.connected == []
    assert flavor.classes.listed[0]["template"] == "support-bot"
    result = flavor.call(fleet.invoke("hola"))
    assert flavor.classes.connected == [sandbox_id_of(1)]
    assert decode_handle(result.session_id).sandbox_id == sandbox_id_of(1)  # type: ignore[union-attr]


def test_a_reconnected_fleet_starts_a_parked_vm_that_has_no_service(flavor: FleetFlavor) -> None:
    flavor.classes.add(served=False, state="SUSPENDED")
    fleet = flavor.call(
        (AsyncAgentFleet if flavor.asynchronous else AgentFleet).connect(
            "support-bot",
            access_token=TOKEN,
            project=project(),
            directory=flavor.classes_directory(),
            transport_factory=flavor.backend.async_factory
            if flavor.asynchronous
            else flavor.backend.factory,
            service=flavor.service(project()),
        )
    )

    result = flavor.call(fleet.invoke("hola"))

    assert result.text == "respuesta a hola"
    assert flavor.classes.sandboxes[sandbox_id_of(1)].served


def test_a_reconnected_fleet_without_a_project_cannot_start_a_parked_vm(
    flavor: FleetFlavor,
) -> None:
    flavor.classes.add(served=False, state="SUSPENDED")
    fleet = flavor.call(
        (AsyncAgentFleet if flavor.asynchronous else AgentFleet).connect(
            "support-bot", access_token=TOKEN, directory=flavor.classes_directory()
        )
    )

    with pytest.raises(InvalidArgumentException):
        flavor.call(fleet.invoke("hola"))


# ----------------------------------------------------------------------- stop


def test_stop_terminates_without_opening_the_members_and_raises_the_first_failure(
    flavor: FleetFlavor,
) -> None:
    first = flavor.classes.add()
    second = flavor.classes.add()
    first.kill_error = SandboxException("falló")
    fleet = flavor.call(
        (AsyncAgentFleet if flavor.asynchronous else AgentFleet).connect(
            "support-bot", access_token=TOKEN, directory=flavor.classes_directory()
        )
    )

    with pytest.raises(SandboxException, match="falló"):
        flavor.call(fleet.stop())

    assert flavor.classes.connected == []
    assert second.state == "TERMINATED"
    assert first.kills == 1


def test_connect_never_counts_a_running_vm_that_is_not_an_agent(flavor: FleetFlavor) -> None:
    mine = flavor.classes.add()
    other = flavor.classes.add(foreign=True)
    fleet = flavor.call(
        (AsyncAgentFleet if flavor.asynchronous else AgentFleet).connect(
            "support-bot", access_token=TOKEN, directory=flavor.classes_directory()
        )
    )

    assert fleet.sandbox_ids == (mine.sandbox_id,)
    flavor.call(fleet.stop())

    assert mine.state == "TERMINATED"
    assert other.state == "RUNNING" and other.kills == 0


def test_an_image_shared_with_other_sandboxes_stops_nothing_that_cannot_be_probed(
    flavor: FleetFlavor,
) -> None:
    flavor.classes.add(foreign=True)
    parked = flavor.classes.add(state="SUSPENDED")
    fleet = flavor.call(
        (AsyncAgentFleet if flavor.asynchronous else AgentFleet).connect(
            "support-bot", access_token=TOKEN, directory=flavor.classes_directory()
        )
    )

    assert fleet.sandbox_ids == ()
    flavor.call(fleet.stop())

    assert parked.kills == 0


def test_a_suspended_vm_counts_while_the_image_only_has_agents(flavor: FleetFlavor) -> None:
    flavor.classes.add()
    parked = flavor.classes.add(state="SUSPENDED")
    fleet = flavor.call(
        (AsyncAgentFleet if flavor.asynchronous else AgentFleet).connect(
            "support-bot", access_token=TOKEN, directory=flavor.classes_directory()
        )
    )

    assert parked.sandbox_id in fleet.sandbox_ids
    assert flavor.classes.connected == []


# --------------------------------------------------------------------- stream


def test_a_routable_stream_carries_the_handle_and_frees_the_vm_on_close(
    flavor: FleetFlavor,
) -> None:
    fleet = flavor.started(SHARED_TWO)

    events, stream = flavor.stream_events(fleet, "hola")

    done = next(event for event in events if isinstance(event, Done))
    assert any(isinstance(event, Text) for event in events)
    assert decode_handle(done.session_id) is not None
    assert stream.session_id == done.session_id
    member = fleet._members[0]
    assert member.inflight == 0


def test_a_single_vm_stream_keeps_the_plain_session_id(flavor: FleetFlavor) -> None:
    fleet = flavor.started()

    events, _ = flavor.stream_events(fleet, "hola")

    done = next(event for event in events if isinstance(event, Done))
    assert not done.session_id.startswith("rs1.")


# --------------------------------------------------------------------- helpers


def _serve(**options: Any) -> Any:
    from rayito._agent._project_domain import ServeOptions

    return ServeOptions(**options)
