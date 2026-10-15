"""La comprobación en vivo de quien va a actuar. Un token de acceso se valida
sin conexión (firma, caducidad, grupos del propio token), y revocar sesiones
o quitar a alguien de un grupo en Cognito no lo invalida: seguiría valiendo
hasta que caduque (`AccessTokenValidity`). Para que una *acción* (lo único
que cambia cosas) no espere a eso, antes de asumir el rol de acciones se
pregunta a Cognito si el usuario sigue habilitado y sigue en el grupo de
acciones. La lectura sigue siendo sin conexión: un usuario desactivado puede
leer hasta que caduque su token.

Si Cognito no responde no se actúa (`AuthUnavailable`, un 503): cierra en
fallo.
"""

from __future__ import annotations

from typing import Any, Final

from botocore.exceptions import BotoCoreError, ClientError

from dashboard_stack.tokens import AuthUnavailable, Caller
from rayito._limits import DASHBOARD_STACK_ACTIONS_GROUP

USER_NOT_FOUND: Final = "UserNotFoundException"
#: `AdminListGroupsForUser` admite hasta 60 resultados por página.
LIST_GROUPS_PAGE_SIZE: Final = 60


class CognitoDirectory:
    def __init__(
        self, client: Any, user_pool_id: str, actions_group: str = DASHBOARD_STACK_ACTIONS_GROUP
    ) -> None:
        self._client = client
        self._pool = user_pool_id
        self._group = actions_group

    def may_act(self, caller: Caller) -> bool:
        """`True` si el usuario existe, está habilitado y está en el grupo."""
        try:
            return self._enabled(caller) and self._in_group(caller)
        except ClientError as error:
            if error.response.get("Error", {}).get("Code") == USER_NOT_FOUND:
                return False
            raise AuthUnavailable("directory") from None
        except BotoCoreError:
            raise AuthUnavailable("directory") from None

    def _enabled(self, caller: Caller) -> bool:
        user = self._client.admin_get_user(UserPoolId=self._pool, Username=caller.username)
        return bool(user.get("Enabled", False))

    def _in_group(self, caller: Caller) -> bool:
        params: dict[str, Any] = {
            "UserPoolId": self._pool,
            "Username": caller.username,
            "Limit": LIST_GROUPS_PAGE_SIZE,
        }
        while True:
            page = self._client.admin_list_groups_for_user(**params)
            if any(group.get("GroupName") == self._group for group in page.get("Groups", [])):
                return True
            token = page.get("NextToken")
            if not token:
                return False
            params["NextToken"] = token
