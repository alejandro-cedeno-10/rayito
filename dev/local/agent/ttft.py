"""Latencia hasta el primer token de un prompt trivial a Bedrock a través de
la pasarela de secretos, medida desde dentro del sandbox con el boto3 del
venv de deepagents (`ConverseStream`, bearer de marcador que la pasarela
sustituye).

    python ttft.py <repeticiones>

Imprime una línea JSON con cada muestra (segundos hasta el primer
`contentBlockDelta` y hasta el final del stream) y ningún texto del modelo.
"""

from __future__ import annotations

import json
import os
import sys
import time
from typing import Final

import boto3

PROMPT: Final = "Responde sólo: ok"
MAX_OUTPUT_TOKENS: Final = 8


def one_sample(client: object) -> dict[str, float | None]:
    started = time.monotonic()
    response = client.converse_stream(  # type: ignore[attr-defined]
        modelId=os.environ["SPIKE_MODEL"],
        messages=[{"role": "user", "content": [{"text": PROMPT}]}],
        inferenceConfig={"maxTokens": MAX_OUTPUT_TOKENS},
    )
    first: float | None = None
    for event in response["stream"]:
        if first is None and "contentBlockDelta" in event:
            first = time.monotonic() - started
    return {
        "first_token_seconds": None if first is None else round(first, 3),
        "total_seconds": round(time.monotonic() - started, 3),
    }


def main() -> int:
    client = boto3.session.Session(region_name=os.environ["SPIKE_REGION"]).client(
        "bedrock-runtime", endpoint_url=os.environ["SPIKE_GATEWAY_URL"]
    )
    samples = [one_sample(client) for _ in range(int(sys.argv[1]))]
    print(json.dumps({"samples": samples}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
