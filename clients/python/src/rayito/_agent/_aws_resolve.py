"""Los valores por defecto de AWS de `deploy_agent()` (ADR-029, D7): la
región, el bucket del artefacto, la imagen base, el secreto del modelo y el
access token. Cada uno se resuelve en el mismo orden (argumento explícito,
fichero de proyecto, variable de entorno, descubrimiento) y ninguno crea nada
en silencio: el bucket nunca se crea (`CreateBucket` no se llama), un secreto
sólo se crea con `create_secret_from_env=True`, y una imagen base ausente se
publica sólo si el llamante pasó quien la publique.

Este módulo reparte la decisión (puras: `resolve_bucket`, `resolve_token`,
`secret_name_for`, `secret_value_from_env`) y la E/S sobre AWS
(`AwsResolver`: `sts`, `s3` y la imagen base) y sobre `SecretStore`. Construir
el resolvedor no llama a AWS: los clientes son perezosos.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Final, Literal, Protocol

from botocore.exceptions import ClientError

from rayito._account_setup._aws import artifact_stack_gateway
from rayito._account_setup._domain import BUCKET_ENV_VAR as BUCKET_ENV_VAR
from rayito._account_setup._domain import (
    BUCKET_NAME_OUTPUT_KEY,
    with_base_image_hint,
    with_setup_hint,
)
from rayito._account_setup._domain import cdk_bucket_name as cdk_bucket_name
from rayito._agent._project_domain import AgentProject
from rayito._aws import LazyClient
from rayito._aws_region import aws_session, resolve_region
from rayito._images import account_image_arn, client_error_code, image_exists
from rayito._payload import generate_access_token
from rayito._sandbox_base import ACCESS_TOKEN_ENV_VAR
from rayito._secret_gateway._domain import SecretGateway
from rayito._secrets import DEFAULT_SECRET_PREFIX, SecretRef
from rayito._stacks._cloudformation import CloudFormationProvisioner
from rayito._stacks._condition import classify_stack
from rayito.exceptions import InvalidArgumentException, StackException

if TYPE_CHECKING:
    import boto3

#: El nombre convencional del secreto de una pasarela, bajo el prefijo
#: `rayito/` con el que la pasarela lo lee (`SecretCache`) y `SecretStore` lo
#: crea: el secreto queda en `rayito/agents/<proyecto>/<pasarela>`. Es el
#: mismo que escribe la detección (`_detect_project.GATEWAY_SECRET_NAME_TEMPLATE`).
AGENT_SECRET_NAME_TEMPLATE: Final = "agents/{project}/{gateway}"
#: El prefijo con el que la pasarela lee su secreto en tiempo de ejecución
#: (la `SecretCache` compartida de `secrets=`/`gateways=`): `--create-secret`
#: comprueba y crea con el mismo, para que el nombre sea exactamente el que
#: se leerá.
GATEWAY_SECRET_PREFIX: Final = DEFAULT_SECRET_PREFIX
BEARER_PREFIX: Final = "Bearer "
#: Lo que se añade al error de una imagen base que falta cuando la imagen del
#: proyecto ya existe: reutilizarla no necesita la base.
REUSE_TEMPLATE_HINT: Final = (
    "; o reutiliza la imagen '{template}' ya construida sin build: "
    "--no-build (build_image=False)"
)
AUTHORIZATION_HEADER: Final = "authorization"
#: Códigos de `HeadBucket` que dan el bucket por inexistente o ajeno: sin
#: `s3:ListBucket` S3 contesta 403 a un HEAD, y un bucket que no se puede
#: leer tampoco sirve para el artefacto.
BUCKET_MISSING_CODES: Final = frozenset({"404", "NoSuchBucket", "NotFound", "403"})
#: Códigos de acceso denegado de CloudFormation (`AccessDenied`) y de la
#: forma `*Exception`; la misma tabla en TypeScript.
ACCESS_DENIED_CODES: Final = frozenset({"AccessDenied", "AccessDeniedException"})
#: La variable de entorno estándar de cada pasarela de modelo
#: (`testdata/agent-deploy/secret-env.json`, la misma tabla en TypeScript).
GATEWAY_SECRET_ENV_VARS: Final[Mapping[str, str]] = MappingProxyType(
    {
        "bedrock": "AWS_BEARER_TOKEN_BEDROCK",
        "openai": "OPENAI_API_KEY",
        "anthropic": "ANTHROPIC_API_KEY",
        "gemini": "GEMINI_API_KEY",
        "azure_openai": "AZURE_OPENAI_API_KEY",
        "openrouter": "OPENROUTER_API_KEY",
        "groq": "GROQ_API_KEY",
        "mistral": "MISTRAL_API_KEY",
        "deepseek": "DEEPSEEK_API_KEY",
        "xai": "XAI_API_KEY",
        "litellm": "LITELLM_API_KEY",
    }
)


def _is_access_denied(exc: StackException) -> bool:
    """Un `StackException` de `describe` cuya causa (el resumen saneado del
    error de AWS) es un acceso denegado."""
    cause = exc.__cause__
    return any(
        isinstance(code, str) and code in ACCESS_DENIED_CODES
        for code in (getattr(cause, "name", None), getattr(cause, "code", None))
    )


BucketSource = Literal["argument", "project_file", "environment", "discovered"]


@dataclass(frozen=True)
class BucketChoice:
    bucket: str
    source: BucketSource


class DeployAws(Protocol):
    """Lo que `deploy_agent()` pregunta a AWS antes de crear nada: la región
    efectiva, la cuenta (`GetCallerIdentity`), si un bucket existe
    (`HeadBucket`), el bucket de la pila `rayito-m0-artifacts` que creó
    `rayito setup` (`DescribeStacks`) y si una imagen existe
    (`GetMicrovmImage`)."""

    @property
    def resolved_region(self) -> str: ...

    def account_id(self) -> str: ...

    def bucket_exists(self, bucket: str) -> bool: ...

    def artifact_stack_bucket(self) -> str | None: ...

    def image_exists(self, name: str) -> bool: ...


class SecretStorePort(Protocol):
    """Lo que `deploy_agent()` usa de `SecretStore`."""

    def exists(self, name: str) -> bool: ...

    def create(self, name: str, value: str, *, metadata: Mapping[str, str] | None = ...) -> Any: ...


@dataclass(frozen=True)
class ResolvedSecrets:
    """Las pasarelas del proyecto con el nombre de secreto que se usará, y las
    pasarelas cuyo secreto creó esta resolución (para avisar del coste; nunca
    el nombre del secreto)."""

    gateways: Mapping[str, SecretGateway]
    created: tuple[str, ...]


# ------------------------------------------------------------------- puras


def secret_name_for(project: str, gateway: str) -> str:
    """El nombre convencional del secreto de la pasarela `gateway` del
    proyecto `project`."""
    return AGENT_SECRET_NAME_TEMPLATE.format(project=project, gateway=gateway)


def resolve_bucket(
    explicit: str | None,
    project_file: str | None,
    environ: Mapping[str, str],
    discover: Callable[[], str | None],
) -> BucketChoice:
    """El bucket del artefacto: argumento > `[deploy].bucket` del fichero de
    proyecto > `RAYITO_BUCKET` > el bucket de activos del bootstrap de CDK de
    la cuenta y la región (si existe) > el de la pila `rayito-m0-artifacts` de
    `rayito setup` (si existe). Sin ninguno, `InvalidArgumentException`
    con las opciones; nunca se crea un bucket."""
    if explicit:
        return BucketChoice(explicit, "argument")
    if project_file:
        return BucketChoice(project_file, "project_file")
    from_environment = environ.get(BUCKET_ENV_VAR)
    if from_environment:
        return BucketChoice(from_environment, "environment")
    discovered = discover()
    if discovered:
        return BucketChoice(discovered, "discovered")
    raise InvalidArgumentException(
        with_setup_hint(
            "no hay bucket para el artefacto de la imagen: pasa bucket= (--bucket), "
            f"[deploy].bucket en el fichero del proyecto o exporta {BUCKET_ENV_VAR}; "
            "Rayito no crea buckets"
        )
    )


def resolve_token(explicit: str | None, environ: Mapping[str, str]) -> str:
    """El access token de la flota: argumento > `RAYITO_ACCESS_TOKEN` > uno
    nuevo (que quien despliega debe guardar: `AgentFleet.access_token`)."""
    return explicit or environ.get(ACCESS_TOKEN_ENV_VAR) or generate_access_token()


def gateway_env_var(gateway: str, preset: str | None = None) -> str | None:
    """La variable de entorno estándar de la pasarela `gateway`: la de su
    `preset` (`[gateways.<nombre>] preset`, p. ej. `bedrock` para una pasarela
    llamada `model`) y, si el preset no tiene una (`custom`) o no se conoce,
    la del nombre de la pasarela, como en 0.16."""
    by_preset = GATEWAY_SECRET_ENV_VARS.get(preset) if preset else None
    return by_preset or GATEWAY_SECRET_ENV_VARS.get(gateway)


def secret_value_from_env(
    gateway: str, header: str, environ: Mapping[str, str], *, preset: str | None = None
) -> str | None:
    """El valor que se guardaría en el secreto de `gateway` a partir de su
    variable de entorno estándar (`gateway_env_var`): `Bearer <clave>` para
    la cabecera `authorization` (salvo que ya lo traiga) y la clave tal cual
    para las demás. `None` si la variable no está o está vacía."""
    variable = gateway_env_var(gateway, preset)
    value = environ.get(variable, "") if variable else ""
    if not value:
        return None
    if header.lower() == AUTHORIZATION_HEADER and not value.startswith(BEARER_PREFIX):
        return BEARER_PREFIX + value
    return value


def _secret_name_of(value: str | SecretRef) -> str:
    return value.name if isinstance(value, SecretRef) else value


def _renamed(value: str | SecretRef, name: str) -> str | SecretRef:
    return dataclasses.replace(value, name=name) if isinstance(value, SecretRef) else name


def _missing_secret_message(gateway: str, preset: str | None = None) -> str:
    variable = gateway_env_var(gateway, preset)
    creation = (
        f"--create-secret (create_secret_from_env=True) lo crea desde {variable}"
        if variable
        else "crea el secreto tú mismo con SecretStore"
    )
    return (
        f"falta el secreto de la pasarela {gateway!r}: pasa uno que ya exista con "
        f"--secret {gateway}=<nombre> (secret_names), o {creation}"
    )


def _missing_value_message(gateway: str, preset: str | None = None) -> str:
    variable = gateway_env_var(gateway, preset)
    return (
        f"no se puede crear el secreto de la pasarela {gateway!r}: "
        f"la variable {variable} no está definida"
        if variable
        else f"no se puede crear el secreto de la pasarela {gateway!r} desde el entorno"
    )


def _override_target(gateway: str, gateway_value: SecretGateway) -> str:
    if len(gateway_value.headers) != 1:
        raise InvalidArgumentException(
            f"la pasarela {gateway!r} lleva varias cabeceras: --secret no puede elegir cuál"
        )
    return next(iter(gateway_value.headers))


def apply_secret_names(
    gateways: Mapping[str, SecretGateway], secret_names: Mapping[str, str]
) -> dict[str, SecretGateway]:
    """Las pasarelas con el nombre de secreto de `secret_names` (pasarela →
    nombre) en lugar del que traía el proyecto. Una pasarela que no existe es
    un error, no se ignora."""
    unknown = sorted(set(secret_names) - set(gateways))
    if unknown:
        raise InvalidArgumentException(
            f"secret_names nombra pasarelas que el proyecto no tiene: {', '.join(unknown)}"
        )
    result = dict(gateways)
    for gateway, name in secret_names.items():
        original = gateways[gateway]
        header = _override_target(gateway, original)
        headers = {header: _renamed(original.headers[header], name)}
        result[gateway] = dataclasses.replace(original, headers=headers)
    return result


# ------------------------------------------------------------- con E/S


def legacy_secret_name(project: str, gateway: str, name: str) -> str | None:
    """El nombre con el prefijo `rayito/` duplicado que 0.16 escribía en la
    pasarela detectada (`rayito/agents/<proyecto>/<proveedor>` relativo a
    `rayito/`) si `name` es el nombre convencional de esa pasarela; `None`
    para cualquier otro nombre, que nunca se reinterpreta."""
    if name != secret_name_for(project, gateway):
        return None
    return GATEWAY_SECRET_PREFIX + name


def _existing_name(
    project: AgentProject, gateway: str, name: str, store: SecretStorePort
) -> str | None:
    """`name` si el secreto existe (`DescribeSecret`); si no, el nombre de
    0.16 (`legacy_secret_name`) si ése existe, para que un despliegue de 0.16
    hecho con `--create-secret` siga usando su secreto sin crear otro."""
    if store.exists(name):
        return name
    legacy = legacy_secret_name(project.name, gateway, name)
    if legacy is not None and store.exists(legacy):
        return legacy
    return None


def ensure_model_secrets(
    project: AgentProject,
    *,
    secret_names: Mapping[str, str],
    create_from_env: bool,
    store: SecretStorePort,
    environ: Mapping[str, str],
) -> ResolvedSecrets:
    """Comprueba que existe el secreto de cada pasarela del proyecto
    (`DescribeSecret`) y, sólo con `create_from_env`, crea el que falte con el
    valor de la variable estándar del preset de la pasarela (o de su nombre:
    `gateway_env_var`) (`CreateSecret`). `store` usa el mismo prefijo con el
    que la pasarela lee el secreto (`rayito/`), así que el nombre que se
    comprueba y se crea es exactamente el que se leerá. El valor sale de
    `environ` a Secrets Manager y no se escribe en disco ni en logs; los
    errores nombran la pasarela, nunca el secreto."""
    gateways = apply_secret_names(project.gateways, secret_names)
    created: list[str] = []
    for gateway, definition in list(gateways.items()):
        preset = project.gateway_presets.get(gateway)
        headers = dict(definition.headers)
        for header, reference in definition.headers.items():
            name = _secret_name_of(reference)
            existing = _existing_name(project, gateway, name, store)
            if existing is not None:
                headers[header] = _renamed(reference, existing)
                continue
            if not create_from_env:
                raise InvalidArgumentException(_missing_secret_message(gateway, preset))
            value = secret_value_from_env(gateway, header, environ, preset=preset)
            if value is None:
                raise InvalidArgumentException(_missing_value_message(gateway, preset))
            store.create(name, value)
            created.append(gateway)
        if headers != dict(definition.headers):
            gateways[gateway] = dataclasses.replace(definition, headers=headers)
    return ResolvedSecrets(gateways=MappingProxyType(gateways), created=tuple(created))


@dataclass
class AwsResolver:
    """El adaptador de `DeployAws` sobre boto3. Los clientes `sts`, `s3` y
    `lambda-microvms` se crean en la primera llamada que los necesita."""

    region: str | None = None
    session: boto3.session.Session | None = None

    def __post_init__(self) -> None:
        self._sts = LazyClient("sts", region=self.region, session=self.session)
        self._s3 = LazyClient("s3", region=self.region, session=self.session)
        self._microvms = LazyClient("lambda-microvms", region=self.region, session=self.session)
        self._account: str | None = None

    @property
    def resolved_region(self) -> str:
        resolved = (
            resolve_region(self.region, self.session)
            or aws_session(self.session, self.region).region_name
        )
        if not resolved:
            raise InvalidArgumentException(
                "no hay región de AWS: pasa region= (--region) o exporta AWS_REGION"
            )
        return resolved

    def account_id(self) -> str:
        if self._account is None:
            self._account = str(self._sts.get().get_caller_identity()["Account"])
        return self._account

    def bucket_exists(self, bucket: str) -> bool:
        try:
            self._s3.get().head_bucket(Bucket=bucket, ExpectedBucketOwner=self.account_id())
        except ClientError as exc:
            if client_error_code(exc) in BUCKET_MISSING_CODES:
                return False
            raise
        return True

    def artifact_stack_bucket(self) -> str | None:
        """La salida `BucketName` de `rayito-m0-artifacts` si la pila está
        lista; `None` si no existe, no está lista, no tiene la salida o
        `DescribeStacks` responde acceso denegado (sólo lectura: quien no
        tiene permisos de CloudFormation sigue viendo el error de bucket de
        siempre). Cualquier otro fallo (p. ej. `Throttling`) se propaga como
        `StackException`."""
        provisioner = CloudFormationProvisioner(region=self.resolved_region, session=self.session)
        try:
            observed = artifact_stack_gateway(provisioner).observe()
        except StackException as exc:
            if _is_access_denied(exc):
                return None
            raise
        if classify_stack(observed) != "usable":
            return None
        return observed.outputs.get(BUCKET_NAME_OUTPUT_KEY) or None

    def image_exists(self, name: str) -> bool:
        arn = account_image_arn(self.resolved_region, self.account_id(), name)
        return image_exists(_MicrovmClients(self._microvms.get()), arn)


def discover_bucket(aws: DeployAws, region: str) -> str | None:
    """El bucket de activos del bootstrap de CDK de la cuenta y la región, si
    existe (`GetCallerIdentity` y `HeadBucket`) y, si no, el que creó
    `rayito setup` (`DescribeStacks` de `rayito-m0-artifacts`). Nunca se crea."""
    candidate = cdk_bucket_name(aws.account_id(), region)
    if aws.bucket_exists(candidate):
        return candidate
    return aws.artifact_stack_bucket()


@dataclass(frozen=True)
class _MicrovmClients:
    microvms: Any


def ensure_base_image(
    aws: DeployAws,
    base: str,
    publisher: Callable[[], None] | None,
    *,
    ensure: bool,
    template: str | None = None,
) -> bool:
    """Si la imagen base existe. Si no, la publica con `publisher` (que el
    llamante dio y que costó una confirmación suya) y vuelve a comprobar;
    sin `publisher` o con `ensure=False` es `InvalidArgumentException` con el
    comando que la publica (para `rayito-base-caps`, `rayito setup
    --caps-image`, que `rayito setup` no publica por defecto). Si `template`
    (la imagen del proyecto) ya existe, el mensaje dice además cómo
    reutilizarla sin construir. Devuelve si hubo que publicarla."""
    if aws.image_exists(base):
        return False
    if not ensure or publisher is None:
        reuse = (
            REUSE_TEMPLATE_HINT.format(template=template)
            if template is not None and aws.image_exists(template)
            else ""
        )
        raise InvalidArgumentException(
            with_base_image_hint(
                f"la imagen base {base!r} no existe en esta cuenta y región y el build "
                "la necesita: publícala con "
                "`rayito image publish --os-capabilities ALL --artifact <zip> "
                "--base-image-version latest` (ver la guía de instalación), pasa "
                f"base_publisher= a deploy_agent() o elige otra con [image] base{reuse}",
                base,
            )
        )
    publisher()
    if not aws.image_exists(base):
        raise InvalidArgumentException(f"la imagen base {base!r} sigue sin existir tras publicarla")
    return True
