"""`plan_session_sweep` (el sweeper de los e2e de Python): la sesión sólo
termina lo que ella misma creó; el resto se informa por número, sin ids."""

from __future__ import annotations

from tests.e2e.session_sweep import plan_session_sweep


def test_only_the_microvms_the_session_created_are_terminated() -> None:
    plan = plan_session_sweep(
        baseline={"before"}, registered=["mine-b", "mine-a"], remaining=["before", "mine-a"]
    )
    assert plan.terminate == ("mine-a",)
    assert plan.preexisting == 1
    assert plan.unregistered == 0


def test_a_microvm_born_during_the_session_outside_its_plane_is_left_alive() -> None:
    plan = plan_session_sweep(
        baseline=set(), registered=["mine"], remaining=["mine", "someone-else"]
    )
    assert plan.terminate == ("mine",)
    assert plan.unregistered == 1
    note = plan.report()
    assert note is not None
    assert "someone-else" not in note
    assert "1 nacidos durante ella" in note


def test_the_ids_to_terminate_come_in_a_stable_order() -> None:
    plan = plan_session_sweep(baseline=set(), registered=["c", "a", "b"], remaining=["b", "c", "a"])
    assert plan.terminate == ("a", "b", "c")


def test_a_baseline_microvm_is_never_terminated_even_if_registered() -> None:
    plan = plan_session_sweep(baseline={"x"}, registered=["x"], remaining=["x"])
    assert plan.terminate == ()
    assert plan.preexisting == 1


def test_nothing_left_means_nothing_to_report() -> None:
    plan = plan_session_sweep(baseline={"x"}, registered=["mine"], remaining=[])
    assert plan.terminate == ()
    assert plan.report() is None
