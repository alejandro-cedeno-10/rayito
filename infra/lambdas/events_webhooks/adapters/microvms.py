"""`lambda-microvms` client for the reconciler (decision 8 of the M15
architecture): the Lambda runtime's own `boto3` has never heard of this
service, so `scripts/gen_stack_assets.py` injects `docs/aws-api/service-2.json`
into the zip as ``models/lambda-microvms/<api-version>/service-2.json``.
`client_from_bundled_model` builds the client from a dedicated botocore
session whose `data_path` is that directory (`infra/events-webhooks.yaml`
also sets `AWS_DATA_PATH` to it for the reconciler).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Final

import boto3
import botocore.session

#: The service name the bundled model is registered under (its directory).
SERVICE_NAME: Final = "lambda-microvms"

#: `MicrovmState` (`models/lambda-microvms/2025-09-09/service-2.json`): a VM
#: the platform has started killing or finished killing. Mirrors the SDK's
#: own `rayito._limits.TERMINAL_STATES` — `ListMicrovms` keeps listing a VM
#: for a while after it reaches one of these, so counting it as still "open"
#: here would mean the reconciler never synthesizes its `killed` event.
TERMINAL_STATES: Final = frozenset({"TERMINATING", "TERMINATED"})


def client_from_bundled_model(models_dir: Path, *, region_name: str | None = None) -> Any:
    """A `lambda-microvms` client from a fresh botocore session: its loader
    is created on first use, after `data_path` is set here, so the bundled
    model is found whatever other `boto3` client or session this execution
    environment built before."""
    core_session = botocore.session.get_session()
    core_session.set_config_variable("data_path", str(models_dir))
    session = boto3.session.Session(botocore_session=core_session, region_name=region_name)
    return session.client(SERVICE_NAME)


class ListMicrovmsLister:
    def __init__(self, client: Any) -> None:
        self._client = client

    def running_sandbox_ids(self) -> set[str]:
        """Every `microvmId` `ListMicrovms` currently reports whose `state`
        is not terminal (that is the sandbox id the rest of the SDK uses),
        paginated. `PENDING`/`RUNNING`/`SUSPENDING`/`SUSPENDED` all count as
        "open" (a suspended sandbox has not been killed); a missing id, or
        one left listed only in a terminal state, means killed."""
        ids: set[str] = set()
        kwargs: dict[str, Any] = {}
        while True:
            page = self._client.list_microvms(**kwargs)
            ids.update(
                item["microvmId"]
                for item in page.get("items", [])
                if item["state"] not in TERMINAL_STATES
            )
            next_token = page.get("nextToken")
            if not next_token:
                return ids
            kwargs["nextToken"] = next_token
