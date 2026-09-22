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
6. **Red**: `vpc-`, `subnet-`, `sg-` y `eni-` con 8 o 17 hex, salvo el
   marcador `0123456789abcdef0`.
7. **Ruta local**: `<unidad>:\\` o `<unidad>:/` seguido de `Users`, `tools` o
   `Projects`, y las mismas carpetas en la forma de Git Bash
   (`/<unidad>/Users/…`).
8. **Access key de AWS**: `AKIA`/`ASIA` y 16 mayúsculas o dígitos, salvo las
   que acaban en `EXAMPLE` (la de la documentación de AWS).

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
"""

from __future__ import annotations

import re
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

PLACEHOLDER_ACCOUNTS = frozenset(
    {"123456789012", "000000000000", "111122223333", "444455556666"}
)
PLACEHOLDER_NETWORK_SUFFIX = "0123456789abcdef0"
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
NETWORK_REASON = (
    "ID de VPC, subnet, security group o ENI real (usa <prefijo>-0123456789abcdef0)"
)
LOCAL_PATH_REASON = "ruta local de una máquina (usa rutas relativas al repo o el nombre de la herramienta)"
ACCESS_KEY_REASON = "access key id de AWS"

Finding = tuple[int, str]


@dataclass(frozen=True)
class Rule:
    reason: str
    pattern: re.Pattern[str]
    is_placeholder: Callable[[re.Match[str]], bool]


def never(_: re.Match[str]) -> bool:
    return False


def placeholder_account(match: re.Match[str]) -> bool:
    return match.group("account") in PLACEHOLDER_ACCOUNTS


def fake_microvm(match: re.Match[str]) -> bool:
    return FAKE_UUID.fullmatch(match.group("uuid")) is not None


def placeholder_network(match: re.Match[str]) -> bool:
    return match.group("suffix") == PLACEHOLDER_NETWORK_SUFFIX


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
        re.compile(r"\b(?:vpc|subnet|sg|eni)-(?P<suffix>[0-9a-f]{17}|[0-9a-f]{8})\b"),
        placeholder_network,
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
    Rule(ACCESS_KEY_REASON, re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"), example_key),
)


def line_reasons(line: str) -> list[str]:
    reasons: list[str] = []
    for rule in RULES:
        if rule.reason in reasons:
            continue
        if any(not rule.is_placeholder(match) for match in rule.pattern.finditer(line)):
            reasons.append(rule.reason)
    return reasons


def findings_in_text(text: str) -> list[Finding]:
    return [
        (number, reason)
        for number, line in enumerate(text.splitlines(), start=1)
        for reason in line_reasons(line)
    ]


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
    paths = [path for path in resolve_paths(argv, base) if path.is_file()]
    total = 0
    scanned = 0
    for path in paths:
        text = readable_text(path)
        if text is None:
            continue
        scanned += 1
        for number, reason in findings_in_text(text):
            total += 1
            print(f"KO {displayed(path, base)}:{number}: {reason}")
    if total:
        print(f"KO {total} hallazgo(s) de identificadores del entorno")
        return 1
    print(f"OK {scanned} fichero(s) de texto sin identificadores del entorno")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
