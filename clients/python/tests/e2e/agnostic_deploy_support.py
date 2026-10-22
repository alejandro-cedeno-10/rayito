"""Lo común a `test_agnostic_deploy.py` (ADR-033): los proyectos de ejemplo
de `testdata/` copiados con un nombre propio de la ejecución, los secretos
que crea la prueba y las comprobaciones de que un valor no aparece donde no
debe (entorno del runtime, imagen y logs).

Variables de entorno, además de `RAYITO_E2E=1` y `RAYITO_TEMPLATE`:

- `RAYITO_E2E_AGNOSTIC=1`: sin ella todo se salta (despliega agentes de verdad
  y cuesta dinero).
- `RAYITO_E2E_RUN_ID`: 8 minúsculas o dígitos; va en el nombre de cada agente,
  imagen y secreto, y la limpieza borra sólo lo que lo lleva.
- AgentCore: `RAYITO_E2E_AGENTCORE=1`, `AWS_REGION`, `RAYITO_BUCKET` y, si una
  SCP de la organización las exige, `RAYITO_E2E_TAGS` (cabecera de
  `agentcore_deploy_support.py`).
- MicroVM: `RAYITO_E2E_ZC_BUCKET`, `RAYITO_E2E_ZC_BASE` (imagen base con
  capabilities desechable) y `RAYITO_E2E_BEDROCK_SECRET` (cabecera de
  `agent_zero_config_support.py`). Sin ellas se saltan los tests de MicroVM.

Ningún grafo llama a un modelo: el modelo que declaran los ejemplos sólo da
forma al plan (el rol o la pasarela), así que la prueba no depende de un LLM.
Los secretos se crean con valores aleatorios y se borran sin recuperación al
terminar; sus valores nunca se imprimen. Las imágenes de MicroVM las borra
quien lanza la ejecución, por inventario antes y después (como en
`agent_zero_config_support.py`). Coste: unos 0,60 USD (seis builds de
CodeBuild y unas pocas sesiones de AgentCore, cuatro versiones de imagen de
MicroVM y unos minutos de VM); el tope, 3 USD.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import shutil
import time
import urllib.request
import zlib
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

import boto3
import pytest

REPO_ROOT: Final = Path(__file__).resolve().parents[4]
TESTDATA: Final = REPO_ROOT / "testdata"
STRUCTURED_SAMPLE: Final = TESTDATA / "agent-structured-samples" / "python" / "langgraph-structured"
SECRET_ENV_SAMPLE: Final = TESTDATA / "agent-secret-env" / "secret-env-echo"
OPENAI_COMPATIBLE_SAMPLE: Final = TESTDATA / "agent-project-e2e" / "py" / "openai-compatible-env"
LANGGRAPH_API_SAMPLE: Final = TESTDATA / "agent-langgraph-api" / "sample"
AGNOSTIC_FLAG_VAR: Final = "RAYITO_E2E_AGNOSTIC"
IGNORED_DIRS: Final = (".venv", "node_modules", "__pycache__", ".git", "expected")
NAME_LINE: Final = re.compile(r'^name = "[^"]*"$', re.MULTILINE)
#: Los grafos de `langgraph-structured` (`triage.py`): un id que empieza por
#: `T-1` es urgente.
TRIAGE_GRAPH: Final = "triage"
URGENT_TICKET: Final = "T-100"
NORMAL_TICKET: Final = "T-200"
#: El modelo de los ejemplos (no se llama nunca): el perfil `us.` de Haiku 4.5.
EXAMPLE_MODEL: Final = "us.anthropic.claude-haiku-4-5-20251001-v1:0"
#: Las variables del ejemplo `secret-env-echo` (`agent.py`) y el resumen que
#: devuelve de cada valor: los 8 primeros caracteres de su SHA-256.
SECRET_ENV_NAMES: Final = ("DATABASE_URL", "TICKETS_TOKEN", "APP_DB_HOST")
DIGEST_CHARS: Final = 8
#: Bytes aleatorios de cada valor de secreto de la prueba (32 en hexadecimal).
SECRET_VALUE_BYTES: Final = 16
#: Un secreto de una pasarela que nunca se llama: el valor no importa.
PLACEHOLDER_GATEWAY_KEY: Final = "Bearer e2e-not-a-real-key"
#: Lo que devuelve el grafo de `openai-compatible-env` (`graph.py`).
OPENAI_COMPATIBLE_BASE_URL: Final = "https://llm-gateway.example.com/v1"
#: El grupo de logs de un runtime (`AWS_API_NOTES.md`, AgentCore Runtime).
RUNTIME_LOG_GROUP_TEMPLATE: Final = "/aws/bedrock-agentcore/runtimes/{runtime_id}-DEFAULT"
SECRET_ENV_LOADED_LINE: Final = "secret_env_loaded count={count}"
SECRET_ENV_DENIED_LINE: Final = "secret_env_failed code=secret_env_denied"
LOG_WAIT_SECONDS: Final = 180.0
LOG_POLL_SECONDS: Final = 10.0
#: Trozo de lectura de una capa de ECR y solape entre trozos, para que un
#: valor partido entre dos trozos también se encuentre.
LAYER_CHUNK_BYTES: Final = 1 << 20
LAYER_OVERLAP_BYTES: Final = 256
LAYER_DOWNLOAD_TIMEOUT_SECONDS: Final = 300.0
GZIP_WINDOW_BITS: Final = 16 + zlib.MAX_WBITS
IMAGE_MANIFEST_TYPES: Final = (
    "application/vnd.docker.distribution.manifest.v2+json",
    "application/vnd.oci.image.manifest.v1+json",
)


def require_agnostic() -> None:
    if os.environ.get(AGNOSTIC_FLAG_VAR) != "1":
        pytest.skip(f"exporta {AGNOSTIC_FLAG_VAR}=1 (cabecera de agnostic_deploy_support.py)")


def staged_project(
    source: Path,
    tmp_path: Path,
    *,
    name: str,
    replace: Mapping[str, str] | None = None,
    extra: Sequence[str] = (),
    base: str | None = None,
    drop_from: str | None = None,
) -> Path:
    """Una copia del ejemplo con el nombre de la ejecución. Si el ejemplo trae
    `rayito-agent.toml`, se le cambia el `name` (y cada texto de `replace`) y,
    con `drop_from`, se corta desde esa tabla hasta el final; si no, se
    escribe uno mínimo. `extra` añade líneas al final y `base`, la imagen base
    de MicroVM."""
    target = tmp_path / name
    shutil.copytree(source, target, ignore=shutil.ignore_patterns(*IGNORED_DIRS))
    project_file = target / "rayito-agent.toml"
    if project_file.exists():
        text = NAME_LINE.sub(f'name = "{name}"', project_file.read_text("utf-8"), count=1)
    else:
        text = f'schema = "rayito.agent-project/1"\nname = "{name}"\n'
    if drop_from is not None:
        assert drop_from in text, drop_from
        text = text.split(drop_from, 1)[0]
    for old, new in (replace or {}).items():
        assert old in text, old
        text = text.replace(old, new)
    lines = [text.rstrip("\n"), *extra]
    if base is not None:
        lines += ["", "[image]", f'base = "{base}"']
    project_file.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return target


def digest(value: str) -> str:
    """El resumen de `secret-env-echo`: comprueba el valor sin imprimirlo."""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:DIGEST_CHARS]


def random_value() -> str:
    return secrets.token_hex(SECRET_VALUE_BYTES)


def tag_list(tags: Mapping[str, str]) -> list[dict[str, str]]:
    return [{"Key": key, "Value": value} for key, value in tags.items()]


@dataclass
class RunSecrets:
    """Los secretos que crea la prueba: se borran todos al terminar, sin
    ventana de recuperación. Guarda los valores para comprobarlos; nunca se
    imprimen."""

    region: str
    tags: Mapping[str, str]
    created: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.client = boto3.session.Session(region_name=self.region).client("secretsmanager")

    def create(self, name: str, value: str | Mapping[str, str]) -> str:
        """Crea el secreto `name` y devuelve su ARN."""
        text = value if isinstance(value, str) else json.dumps(dict(value))
        created = self.client.create_secret(Name=name, SecretString=text, Tags=tag_list(self.tags))
        arn = str(created["ARN"])
        self.created[name] = arn
        return arn

    def rotate(self, name: str, value: str) -> None:
        self.client.put_secret_value(SecretId=name, SecretString=value)

    def delete_all(self) -> None:
        for name in list(self.created):
            self.client.delete_secret(SecretId=name, ForceDeleteWithoutRecovery=True)
            del self.created[name]


def found_in(texts: Iterable[str], values: Iterable[str]) -> list[int]:
    """Las posiciones de `values` que aparecen en algún texto (nunca el
    valor: así un fallo no lo imprime)."""
    wanted = list(values)
    return sorted({index for text in texts for index, value in enumerate(wanted) if value in text})


def runtime_environment(control: Any, runtime_id: str) -> dict[str, str]:
    """`environmentVariables` de `GetAgentRuntime`, tal como lo ve cualquiera
    con permiso de lectura sobre el runtime."""
    described = control.get_agent_runtime(agentRuntimeId=runtime_id)
    return dict(described.get("environmentVariables") or {})


def runtime_log_messages(logs: Any, runtime_id: str) -> list[str]:
    group = RUNTIME_LOG_GROUP_TEMPLATE.format(runtime_id=runtime_id)
    messages: list[str] = []
    try:
        for page in logs.get_paginator("filter_log_events").paginate(logGroupName=group):
            messages += [str(event.get("message", "")) for event in page.get("events", [])]
    except logs.exceptions.ResourceNotFoundException:
        return []
    return messages


def wait_for_log_line(logs: Any, runtime_id: str, line: str) -> list[str]:
    """Los mensajes del grupo de logs del runtime en cuanto alguno contiene
    `line`; los logs de AgentCore llegan con algo de retraso."""
    deadline = time.monotonic() + LOG_WAIT_SECONDS
    while True:
        messages = runtime_log_messages(logs, runtime_id)
        if any(line in message for message in messages):
            return messages
        assert time.monotonic() < deadline, f"sin '{line}' en los logs del runtime"
        time.sleep(LOG_POLL_SECONDS)


def image_holds(ecr: Any, image_uri: str, values: Sequence[str]) -> list[int]:
    """Las posiciones de `values` que aparecen en la imagen de ECR: su
    configuración (entorno, órdenes) y cada capa descomprimida, leída por
    trozos sin Docker. Vacío si no aparece ninguno."""
    repository, tag = _repository_and_tag(image_uri)
    image = ecr.batch_get_image(
        repositoryName=repository,
        imageIds=[{"imageTag": tag}],
        acceptedMediaTypes=list(IMAGE_MANIFEST_TYPES),
    )["images"][0]
    manifest = json.loads(image["imageManifest"])
    needles = [value.encode("utf-8") for value in values]
    found: set[int] = set()
    blobs = [(manifest["config"]["digest"], False)]
    blobs += [(layer["digest"], "gzip" in layer["mediaType"]) for layer in manifest["layers"]]
    for blob_digest, compressed in blobs:
        url = ecr.get_download_url_for_layer(repositoryName=repository, layerDigest=blob_digest)
        found |= _blob_holds(str(url["downloadUrl"]), needles, compressed=compressed)
    return sorted(found)


def _repository_and_tag(image_uri: str) -> tuple[str, str]:
    path = image_uri.split("/", 1)[1]
    repository, _, tag = path.rpartition(":")
    return repository, tag


def _blob_holds(url: str, needles: Sequence[bytes], *, compressed: bool) -> set[int]:
    found: set[int] = set()
    tail = b""
    with urllib.request.urlopen(url, timeout=LAYER_DOWNLOAD_TIMEOUT_SECONDS) as response:
        for chunk in _chunks(response.read, compressed=compressed):
            window = tail + chunk
            found |= {index for index, needle in enumerate(needles) if needle in window}
            tail = window[-LAYER_OVERLAP_BYTES:]
    return found


def _chunks(read: Callable[[int], bytes], *, compressed: bool) -> Iterator[bytes]:
    """Los bytes de un blob, descomprimidos si es una capa gzip (que puede
    traer varios miembros seguidos)."""
    decompressor = zlib.decompressobj(GZIP_WINDOW_BITS) if compressed else None
    while chunk := read(LAYER_CHUNK_BYTES):
        if decompressor is None:
            yield chunk
            continue
        data = decompressor.decompress(chunk)
        while decompressor.eof and decompressor.unused_data:
            rest = decompressor.unused_data
            decompressor = zlib.decompressobj(GZIP_WINDOW_BITS)
            data += decompressor.decompress(rest)
        yield data
    if decompressor is not None:
        yield decompressor.flush()
