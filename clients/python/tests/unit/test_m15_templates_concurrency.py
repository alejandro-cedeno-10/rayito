"""`build_slot` (m15-templates, TPL-1/Q83): guarda local de a lo sumo
`MAX_CONCURRENT_BUILDS` builds en vuelo en este proceso; el siguiente
lanza `BuildException(reason="build_quota")` en el acto."""

from __future__ import annotations

from contextlib import ExitStack

import pytest

from rayito._templates._concurrency import MAX_CONCURRENT_BUILDS, build_slot
from rayito.exceptions import BuildException


def test_up_to_the_limit_of_slots_can_be_held_at_once() -> None:
    with ExitStack() as stack:
        for _ in range(MAX_CONCURRENT_BUILDS):
            stack.enter_context(build_slot())
        with pytest.raises(BuildException) as excinfo, build_slot():
            pass
        assert excinfo.value.reason == "build_quota"


def test_a_released_slot_can_be_reacquired() -> None:
    with build_slot():
        pass
    with ExitStack() as stack:
        for _ in range(MAX_CONCURRENT_BUILDS):
            stack.enter_context(build_slot())
