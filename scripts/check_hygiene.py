"""Comprueba que ningún fichero versionado lleva identificadores del entorno.

El repositorio se publica: nada de lo versionado puede delatar la cuenta de
AWS, los recursos ni la máquina donde se desarrolló. Sin red y sólo con la
biblioteca estándar, recorre cada fichero de `git ls-files` (o los que se le
pasen) línea a línea y falla con estas reglas:

1. **Cuenta en un ARN**: `arn:<partición>:<servicio>:<región>:<12 dígitos>`.
2. **Cuenta en un bucket o registro**: 12 dígitos dentro del nombre de un
   bucket `s3://…` o `arn:aws:s3:::…`, el sufijo `<12 dígitos>-<región>` de
   los buckets de CDK/SAM y el host `<12 dígitos>.dkr.ecr.…`. En estas dos
   reglas sólo pasan los marcadores de la documentación de AWS
   (`PLACEHOLDER_ACCOUNTS`); los ARN de recursos gestionados llevan `aws` en
   el campo de cuenta y nunca casan.
3. **Bucket de bootstrap de CDK**: `cdk-` seguido del qualifier por defecto
   (`CDK_QUALIFIER`); su nombre completo lleva cuenta y región.
4. **Perfil o rol de SSO**: `AdministratorAccess-<dígito>`,
   `<PermissionSet>Access-<12 dígitos>` (los perfiles que crea
   `aws configure sso`) y `AWSReservedSSO_<set>_<16 hex>` (sufijo propio de
   cada cuenta).
5. **MicroVM**: `microvm-` seguido de 8 hex (un UUID completo o un prefijo
   truncado como los de las bitácoras) y los endpoints
   `<uuid>.lambda-microvm.…`. Un ID es falso, y pasa, sólo si su UUID es
   `00000000-0000-0000-0000-` seguido de 12 dígitos decimales
   (`microvm-00000000-0000-0000-0000-000000000001`, …) o si el prefijo
   truncado es `00000000` (el de esos falsos cuando la prosa los abrevia o
   los escribe con un marcador); `microvm-<id>` en prosa no casa.
6. **Recursos de AWS**: `vpc-`, `subnet-`, `sg-`, `eni-`, `rtb-`, `igw-`,
   `acl-`, `pcx-`, `tgw-`, `tgw-attach-`, `vpce-`, `eipalloc-`, `fs-`,
   `fsap-`, `fsmt-`, `nc-`, `ami-`, `snap-` y `vol-` con 8 o 17 hex, y
   `nat-`, `lt-` e `i-` con 17. Un ID real es hex aleatorio; pasa, como
   marcador, el sufijo que contiene `0123` (`vpc-0123456789abcdef0`,
   `fs-0123abcd`) o `abcd` (`fsap-0456abcd`), o que repite un carácter seis
   veces seguidas (`fs-99999999`, `fsap-00000001`). Un ID aleatorio pasa
   por azar una vez de cada ~6.500 (8 hex) o ~2.300 (17 hex).
7. **Ruta local**: `<unidad>:\\` o `<unidad>:/` seguido de `Users`, `tools` o
   `Projects`, las mismas carpetas en la forma de Git Bash
   (`/<unidad>/Users/…`), el home de macOS (`/Users/<usuario>`) y sus
   temporales (`tmp` y `var` bajo `/private/`).
8. **Access key de AWS**: `AKIA`/`ASIA` y 16 mayúsculas o dígitos, salvo las
   que acaban en `EXAMPLE` (la de la documentación de AWS).
9. **Cuenta en un campo de cuenta**: 12 dígitos tras `account`,
   `accountId`, `account_id`, `sso_account_id`, `"Account":` o
   `--account-id`. En esta regla y en las 1 y 2 pasan también las cuentas de
   un solo dígito repetido (`111111111111`, `999999999999`).
10. **Portal de SSO**: `<algo>.awsapps.com` y `<algo>.portal.<región>.app.aws`
    (las URLs de inicio de IAM Identity Center), `ssoins-<16 hex>` y el
    identity store `d-<10 hex>`; pasan `my-sso-portal` y `d-xxxxxxxxxx`, los
    marcadores de la documentación de AWS.
11. **URL prefirmada o credencial temporal**: `X-Amz-Signature=` con los 64
    hex de una firma SigV4, `X-Amz-Security-Token=` con 40 o más caracteres
    y el principio de un session token de STS (`IQoJb3JpZ2lu…`,
    `FwoGZXIvYXdz…`).
12. **Token**: un JWT o JWE compacto (`eyJ…` con sus segmentos separados por
    puntos) y los tokens de GitHub (`ghp_`, `gho_`, `ghu_`, `ghs_`, `ghr_`,
    `github_pat_`), npm (`npm_`), PyPI (`pypi-AgE…`) y Slack (`xox?-`).
13. **Lista privada**: nombres de empresa, dominios, cuentas o cualquier otro
    término que no puede aparecer pero que tampoco puede escribirse aquí,
    porque listarlo sería publicarlo. Se leen, sin distinguir mayúsculas, de
    la variable de entorno `RAYITO_HYGIENE_DENYLIST` (separados por comas o
    saltos de línea; en CI es un secreto del repositorio) y del fichero
    `.git/info/hygiene-denylist` (uno por línea, `#` comenta), que Git nunca
    versiona. Sin ninguno de los dos la regla no hace nada; el hallazgo nunca
    dice qué término casó.

Los ficheros binarios (un byte NUL o UTF-8 inválido) se saltan, como hace
`git grep -I`; los lockfiles se recorren como cualquier otro fichero. Cada
hallazgo imprime fichero, línea y regla, nunca la línea: el gate no puede
copiar una access key al log de CI. El qualifier de CDK va en una constante
aparte y los tests construyen sus muestras por piezas, así que este módulo y
sus tests se recorren a sí mismos sin lista de exclusiones. Los nombres de
usuario o de carpetas personales no están aquí porque listarlos sería
publicarlos; las reglas de rutas los cazan donde aparecen.

    python scripts/check_hygiene.py              # todo `git ls-files`
    python scripts/check_hygiene.py FICHERO...   # otros caminos (tests)
    printf '%s' "$TEXTO" | python scripts/check_hygiene.py -   # stdin (título y cuerpo de un PR)
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

PLACEHOLDER_ACCOUNTS = frozenset(
    {"123456789012", "000000000000", "111122223333", "444455556666"}
)
REPEATED_DIGIT_ACCOUNT = re.compile(r"([0-9])\1{11}")
PLACEHOLDER_RESOURCE_SUFFIX = re.compile(r"0123|abcd|(.)\1{5}")
PLACEHOLDER_SSO_PORTALS = frozenset({"my-sso-portal", "d-xxxxxxxxxx"})
DENYLIST_ENV = "RAYITO_HYGIENE_DENYLIST"
DENYLIST_GIT_PATH = "info/hygiene-denylist"
STDIN_ARGUMENT = "-"
STDIN_NAME = "<stdin>"
EXAMPLE_KEY_SUFFIX = "EXAMPLE"
CDK_QUALIFIER = "hnb659fds"
FAKE_UUID = re.compile(r"00000000(?:-0000-0000-0000-[0-9]{12})?")
BINARY_MARKER = b"\0"
REGION = r"(?:af|ap|ca|eu|il|me|mx|sa|us)(?:-gov)?-[a-z]+-[0-9]"
UUID = r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"

ACCOUNT_IN_ARN_REASON = "ID de cuenta de AWS en un ARN (usa 123456789012)"
ACCOUNT_IN_NAME_REASON = (
    "ID de cuenta de AWS en un nombre de bucket o registro (usa amzn-s3-demo-bucket)"
)
CDK_BUCKET_REASON = (
    "bucket de bootstrap de CDK, que lleva cuenta y región (usa amzn-s3-demo-bucket)"
)
SSO_REASON = "perfil o rol de SSO con datos de la cuenta (usa <tu-perfil>)"
MICROVM_REASON = "ID o endpoint de MicroVM real (usa microvm-<id> o microvm-00000000-0000-0000-0000-<12 dígitos>)"
NETWORK_REASON = "ID de recurso de AWS real: VPC, subnet, SG, ENI, rutas, gateways, EFS, AMI… (usa <prefijo>-0123456789abcdef0)"
LOCAL_PATH_REASON = "ruta local de una máquina (usa rutas relativas al repo o el nombre de la herramienta)"
ACCESS_KEY_REASON = "access key id de AWS"
ACCOUNT_FIELD_REASON = "ID de cuenta de AWS en un campo de cuenta (usa 123456789012)"
SSO_PORTAL_REASON = (
    "portal o instancia de IAM Identity Center (usa my-sso-portal.awsapps.com)"
)
PRESIGNED_REASON = "firma de URL prefirmada o session token de STS"
TOKEN_REASON = "token (JWT/JWE, GitHub, npm, PyPI o Slack)"
PRIVATE_TERM_REASON = (
    "término de la lista privada (empresa, dominio, cuenta u organización)"
)

Finding = tuple[int, str]


@dataclass(frozen=True)
class Rule:
    reason: str
    pattern: re.Pattern[str]
    is_placeholder: Callable[[re.Match[str]], bool]


def never(_: re.Match[str]) -> bool:
    return False


def placeholder_account(match: re.Match[str]) -> bool:
    account = match.group("account")
    return (
        account in PLACEHOLDER_ACCOUNTS
        or REPEATED_DIGIT_ACCOUNT.fullmatch(account) is not None
    )


def fake_microvm(match: re.Match[str]) -> bool:
    return FAKE_UUID.fullmatch(match.group("uuid")) is not None


def placeholder_resource(match: re.Match[str]) -> bool:
    suffix = match.group("suffix") or match.group("long_suffix")
    return PLACEHOLDER_RESOURCE_SUFFIX.search(suffix) is not None


def placeholder_sso_portal(match: re.Match[str]) -> bool:
    return (match.group("portal") or "").lower() in PLACEHOLDER_SSO_PORTALS


def example_key(match: re.Match[str]) -> bool:
    return match.group(0).endswith(EXAMPLE_KEY_SUFFIX)


RULES: tuple[Rule, ...] = (
    Rule(
        ACCOUNT_IN_ARN_REASON,
        re.compile(
            r"\barn:aws[a-z-]*:[a-z0-9-]+:[a-z0-9-]*:(?P<account>[0-9]{12})(?![0-9])"
        ),
        placeholder_account,
    ),
    Rule(
        ACCOUNT_IN_NAME_REASON,
        re.compile(
            r"(?:s3://|arn:aws[a-z-]*:s3:::)[a-z0-9.-]*?(?<![0-9])(?P<account>[0-9]{12})(?![0-9])"
        ),
        placeholder_account,
    ),
    Rule(
        ACCOUNT_IN_NAME_REASON,
        re.compile(rf"(?<![0-9])(?P<account>[0-9]{{12}})-{REGION}\b"),
        placeholder_account,
    ),
    Rule(
        ACCOUNT_IN_NAME_REASON,
        re.compile(r"(?<![0-9])(?P<account>[0-9]{12})\.dkr\.ecr\."),
        placeholder_account,
    ),
    Rule(CDK_BUCKET_REASON, re.compile("cdk-" + CDK_QUALIFIER), never),
    Rule(
        SSO_REASON,
        re.compile(
            r"\bAdministratorAccess-[0-9]|\b[A-Za-z]+Access-[0-9]{12}\b"
            r"|\bAWSReservedSSO_[\w+=,.@-]+_[0-9a-f]{16}\b"
        ),
        never,
    ),
    Rule(
        MICROVM_REASON,
        re.compile(rf"microvm-(?P<uuid>{UUID}|[0-9a-fA-F]{{8}})(?![0-9a-zA-Z])"),
        fake_microvm,
    ),
    Rule(
        MICROVM_REASON,
        re.compile(rf"(?<![0-9a-fA-F-])(?P<uuid>{UUID})\.lambda-microvm\."),
        fake_microvm,
    ),
    Rule(
        NETWORK_REASON,
        re.compile(
            r"\b(?:vpc|subnet|sg|eni|rtb|igw|acl|pcx|tgw|tgw-attach|vpce|eipalloc"
            r"|fs|fsap|fsmt|nc|ami|snap|vol)-(?P<suffix>[0-9a-f]{17}|[0-9a-f]{8})\b"
            r"|\b(?:nat|lt|i)-(?P<long_suffix>[0-9a-f]{17})\b"
        ),
        placeholder_resource,
    ),
    Rule(
        LOCAL_PATH_REASON,
        re.compile(r"\b[A-Za-z]:[\\/]+(?:Users|tools|Projects)\b"),
        never,
    ),
    Rule(
        LOCAL_PATH_REASON,
        re.compile(r"(?<![\w.-])/[A-Za-z]/(?:Users|tools|Projects)\b"),
        never,
    ),
    Rule(
        LOCAL_PATH_REASON,
        re.compile(r"(?<![\w.-])/(?:Users/[A-Za-z0-9._-]+|private/(?:tmp|var)/)"),
        never,
    ),
    Rule(ACCESS_KEY_REASON, re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"), example_key),
    Rule(
        ACCOUNT_FIELD_REASON,
        re.compile(
            r"(?i)(?:account(?:[_-]?id)?[\"']?\s*[:=]\s*[\"']?|--account-id[\s=]+)"
            r"(?P<account>[0-9]{12})(?![0-9])"
        ),
        placeholder_account,
    ),
    Rule(
        SSO_PORTAL_REASON,
        re.compile(
            r"(?i)(?<![\w.-])(?P<portal>[a-z0-9][a-z0-9-]*)\.awsapps\.com\b"
            r"|(?i:\b[a-z0-9-]+\.portal\.[a-z0-9-]+\.app\.aws\b)"
            r"|\bssoins-[0-9a-f]{16}\b|\bd-[0-9a-f]{10}\b"
        ),
        placeholder_sso_portal,
    ),
    Rule(
        PRESIGNED_REASON,
        re.compile(
            r"X-Amz-Signature=[0-9a-f]{64}\b"
            r"|X-Amz-Security-Token=[A-Za-z0-9%/+=_-]{40,}"
            r"|\b(?:IQoJb3JpZ2lu|FwoGZXIvYXdz)[A-Za-z0-9+/=]{40,}"
        ),
        never,
    ),
    Rule(
        TOKEN_REASON,
        re.compile(
            r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]*\.[A-Za-z0-9_-]{8,}"
            r"|\bgh[pousr]_[A-Za-z0-9]{36}\b|\bgithub_pat_[A-Za-z0-9_]{22,}"
            r"|\bnpm_[A-Za-z0-9]{36}\b|\bpypi-AgE[A-Za-z0-9_-]{20,}"
            r"|\bxox[abprs]-[A-Za-z0-9-]{10,}"
        ),
        never,
    ),
)


def line_reasons(line: str, private_terms: tuple[str, ...] = ()) -> list[str]:
    reasons: list[str] = []
    for rule in RULES:
        if rule.reason in reasons:
            continue
        if any(not rule.is_placeholder(match) for match in rule.pattern.finditer(line)):
            reasons.append(rule.reason)
    folded = line.casefold()
    if any(term in folded for term in private_terms):
        reasons.append(PRIVATE_TERM_REASON)
    return reasons


def findings_in_text(text: str, private_terms: tuple[str, ...] = ()) -> list[Finding]:
    return [
        (number, reason)
        for number, line in enumerate(text.splitlines(), start=1)
        for reason in line_reasons(line, private_terms)
    ]


def parse_terms(raw: str, *, comments: bool) -> list[str]:
    """Los términos de una lista privada: separados por comas o saltos de
    línea, sin espacios alrededor, en minúsculas plegadas y sin vacíos; con
    `comments`, una línea que empieza por `#` se ignora."""
    terms: list[str] = []
    for line in raw.splitlines():
        if comments and line.lstrip().startswith("#"):
            continue
        terms.extend(part.strip().casefold() for part in line.split(","))
    return [term for term in terms if term]


def denylist_file(root: Path) -> Path | None:
    """`.git/info/hygiene-denylist` del repositorio de `root` (también desde un
    worktree), o `None` si `root` no está en un repositorio de Git."""
    result = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "--git-path", DENYLIST_GIT_PATH],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        return None
    path = Path(result.stdout.strip())
    return path if path.is_absolute() else root / path


def private_terms(root: Path, environ: dict[str, str] | None = None) -> tuple[str, ...]:
    """La lista privada: la variable `RAYITO_HYGIENE_DENYLIST` más el fichero
    `.git/info/hygiene-denylist`, sin duplicados. Nunca se imprime."""
    env = os.environ if environ is None else environ
    terms = parse_terms(env.get(DENYLIST_ENV, ""), comments=False)
    path = denylist_file(root)
    if path is not None and path.is_file():
        terms.extend(parse_terms(path.read_text(encoding="utf-8"), comments=True))
    return tuple(dict.fromkeys(terms))


def readable_text(path: Path) -> str | None:
    """El contenido de un fichero de texto, o `None` si es binario: un byte NUL
    o UTF-8 inválido, el mismo criterio que `git grep -I`."""
    data = path.read_bytes()
    if BINARY_MARKER in data:
        return None
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return None


def tracked_files(root: Path) -> list[Path]:
    listing = subprocess.run(
        ["git", "-C", str(root), "ls-files", "-z"], capture_output=True, check=True
    ).stdout
    names = listing.decode("utf-8", errors="surrogateescape").split("\0")
    return [root / name for name in names if name]


def resolve_paths(argv: list[str], root: Path) -> list[Path]:
    if argv:
        return [Path(argument) for argument in argv]
    return tracked_files(root)


def displayed(path: Path, root: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return path.as_posix()


def main(argv: list[str], root: Path | None = None) -> int:
    base = root or Path.cwd()
    terms = private_terms(base)
    read_stdin = STDIN_ARGUMENT in argv
    files = [argument for argument in argv if argument != STDIN_ARGUMENT]
    paths = (
        [path for path in resolve_paths(files, base) if path.is_file()]
        if files or not read_stdin
        else []
    )
    sources: list[tuple[str, str | None]] = [
        (displayed(path, base), readable_text(path)) for path in paths
    ]
    if read_stdin:
        sources.append((STDIN_NAME, sys.stdin.read()))
    total = 0
    scanned = 0
    for name, text in sources:
        if text is None:
            continue
        scanned += 1
        for number, reason in findings_in_text(text, terms):
            total += 1
            print(f"KO {name}:{number}: {reason}")
    if total:
        print(f"KO {total} hallazgo(s) de identificadores del entorno")
        return 1
    print(f"OK {scanned} fichero(s) de texto sin identificadores del entorno")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
