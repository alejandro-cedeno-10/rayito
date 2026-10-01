"""`lambda-microvms` client for the reconciler (decision 8 of the M15
architecture): the Lambda runtime's own `boto3` has never heard of this
service, so the zip bundles `docs/aws-api/service-2.json` under
``models/lambda-microvms/<api-version>/service-2.json`` and the handler
points `AWS_DATA_PATH` at that directory before constructing the client —
see `handlers/reconciler.py` and `infra/events-webhooks.yaml`'s
`AWS_DATA_PATH` environment variable.
"""

from __future__ import annotations

from typing import Any


class ListMicrovmsLister:
    def __init__(self, client: Any) -> None:
        self._client = client

    def running_sandbox_ids(self) -> set[str]:
        """Every `microvmId` `ListMicrovms` currently reports (that is the
        sandbox id the rest of the SDK uses), paginated. `RUNNING` and
        `SUSPENDED` both count as "open" (a suspended sandbox has not been
        killed); only a missing id means killed."""
        ids: set[str] = set()
        kwargs: dict[str, Any] = {}
        while True:
            page = self._client.list_microvms(**kwargs)
            ids.update(item["microvmId"] for item in page.get("items", []))
            next_token = page.get("nextToken")
            if not next_token:
                return ids
            kwargs["nextToken"] = next_token
