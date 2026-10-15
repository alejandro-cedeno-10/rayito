"""La comprobación en vivo de quien actúa: habilitado y en el grupo."""

from __future__ import annotations

from typing import Any

import _stack_path  # noqa: F401
import pytest
from botocore.exceptions import BotoCoreError, ClientError
from dashboard_stack.directory import CognitoDirectory
from dashboard_stack.tokens import AuthUnavailable, Caller

from rayito._limits import DASHBOARD_STACK_ACTIONS_GROUP

CALLER = Caller("subject-0001", "user-0001", frozenset({DASHBOARD_STACK_ACTIONS_GROUP}))


class Cognito:
    def __init__(
        self,
        *,
        enabled: bool = True,
        groups: tuple[str, ...] = (DASHBOARD_STACK_ACTIONS_GROUP,),
        error: Exception | None = None,
    ) -> None:
        self.enabled = enabled
        self.groups = groups
        self.error = error
        self.usernames: list[str] = []

    def admin_get_user(self, *, UserPoolId: str, Username: str) -> dict[str, Any]:
        self.usernames.append(Username)
        if self.error is not None:
            raise self.error
        return {"Enabled": self.enabled}

    def admin_list_groups_for_user(self, **params: Any) -> dict[str, Any]:
        if "NextToken" not in params and len(self.groups) > 1:
            return {"Groups": [{"GroupName": self.groups[0]}], "NextToken": "t"}
        return {"Groups": [{"GroupName": name} for name in self.groups[1:] or self.groups]}


def directory(cognito: Cognito) -> CognitoDirectory:
    return CognitoDirectory(cognito, "us-east-1_AbCdEfGhI")


def test_an_enabled_member_of_the_group_may_act() -> None:
    cognito = Cognito()
    assert directory(cognito).may_act(CALLER)
    assert cognito.usernames == ["user-0001"]


def test_a_disabled_user_or_one_removed_from_the_group_may_not_act() -> None:
    assert not directory(Cognito(enabled=False)).may_act(CALLER)
    assert not directory(Cognito(groups=("otro",))).may_act(CALLER)


def test_the_group_is_found_on_a_later_page() -> None:
    assert directory(Cognito(groups=("otro", DASHBOARD_STACK_ACTIONS_GROUP))).may_act(CALLER)


def test_a_deleted_user_may_not_act() -> None:
    gone = ClientError({"Error": {"Code": "UserNotFoundException"}}, "AdminGetUser")
    assert not directory(Cognito(error=gone)).may_act(CALLER)


@pytest.mark.parametrize(
    "error",
    [ClientError({"Error": {"Code": "AccessDeniedException"}}, "AdminGetUser"), BotoCoreError()],
)
def test_if_cognito_cannot_answer_the_caller_may_not_act(error: Exception) -> None:
    with pytest.raises(AuthUnavailable):
        directory(Cognito(error=error)).may_act(CALLER)
