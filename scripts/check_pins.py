"""Comprueba que todo lo que la automatización de este repo ejecuta está clavado.

Seis puertas, todas en lista blanca (fallan salvo que la línea demuestre estar
clavada), sin red y sólo con la biblioteca estándar:

1. **Acciones**: cada `uses:` de `.github/workflows/` (y de las acciones
   locales `.github/actions/*/action.yml`) tiene que nombrar un SHA
   de 40 hex, con un comentario opcional con la etiqueta de la que salió
   (`owner/repo@<sha> # vX.Y.Z`). Una etiqueta, un alias de mayor, una rama o
   un SHA truncado son hallazgos; sólo se saltan las líneas comentadas y las
   acciones locales (`./...`).
2. **uvx**: cada invocación de `uvx` en esos workflows y en el `Makefile` tiene
   que nombrar su herramienta con `==` (`uvx ruff==0.16.7 check ...`, también
   en la forma `uvx --from paquete==1.2.3 orden`), porque resuelven y ejecutan
   código de terceros dentro de trabajos que llevan credenciales de publicación.
   Y `==` sólo fija la herramienta, no su grafo: cada run resuelve la última
   versión de cada dependencia transitiva, sin hash ni cooldown
   (`sec-supply-chain-followups`, SC-A03). Por eso sólo pasan por `uvx` las
   herramientas sin dependencias de `DEPENDENCY_FREE_UVX_TOOLS` (hoy `ruff`,
   un binario sin dependencias de Python); las demás se instalan desde un
   fichero de requisitos con `--hash` (`.github/release/requirements-*.txt`).
3. **Descargas**: en `image/Dockerfile`, en los del entorno local
   (`dev/local/*/Dockerfile`) y en todo fichero llamado `Dockerfile` que se
   le pase, cada instrucción con `curl` tiene que asignar
   un `<NOMBRE>_SHA256=` de 64 hex en minúsculas y no nombrar una release
   flotante (`/releases/latest`, `/latest/download/`); además cada `curl` de
   la instrucción tiene que escribir a un fichero con `-o`/`--output` (nunca
   a stdout ni a una tubería: `curl ... | sh` es un hallazgo), esa misma ruta
   tiene que aparecer en una orden `sha256sum -c` de la instrucción, y no
   puede haber más `curl` que comprobaciones. `wget` y un `ADD` con URL
   `http(s)://` son hallazgos siempre: la única forma admitida es
   `curl -o` + `sha256sum -c`. Lo descargado entra en la imagen de todos los
   sandboxes, y el sha256 fijado no cambia aunque se mueva una etiqueta
   (SECURITY.md T10). Las líneas de continuación (`\\`) se unen en una sola
   instrucción, contada desde su primera línea; las comentadas se saltan. La
   puerta es léxica: no sigue variables de shell (`-o "$F"` sólo casa con una
   comprobación que nombre `$F` igual) ni interpreta subshells.
4. **dnf**: en esos mismos `Dockerfile`, cada paquete de
   `PINNED_DNF_PACKAGES` (hoy `git-core` y `amazon-efs-utils`) que nombre un
   `dnf install` tiene que llevar `-<versión>-<release>`
   (`git-core-2.50.1-1.amzn2023.0.1`), así
   subirlo es un diff revisable y una versión de imagen nueva. Los demás
   paquetes de la línea siguen sin clavar: la línea base de M1 (SECURITY.md
   T10). El hallazgo cuenta desde la primera línea de la instrucción y cita el
   paquete.
5. **pip**: en esos mismos `Dockerfile`, cada instrucción con un `pip install`
   (o `python3 -m pip install`), con `-r`/`--requirement` o con paquetes
   sueltos, tiene que llevar `--require-hashes`, `--no-deps` y
   `--only-binary=:all:` (`--only-binary all` también vale): sin esas tres
   banderas un fichero añadido a una release existente, o un sdist que
   compile en la VM de build, se instala en silencio (C-12). Un `pip install
   paquete==x` suelto con las tres banderas lo rechaza el propio pip, porque
   `--require-hashes` exige un `--hash=` que solo cabe en un fichero de
   requisitos. Y cada línea de requisito (`nombre==versión
   [--hash=...]...`) de `kernel-sidecar/requirements.txt` y
   `requirements-poly.txt` tiene que llevar al menos un `--hash=sha256:` de 64
   hex; una línea sin ninguno es un hallazgo. El hallazgo del `pip install`
   cuenta desde la primera línea de la instrucción; el de la línea de
   requisito, desde esa misma línea del fichero de pines.

6. **Descargas en workflows** (`sec-supply-chain-ci`): la misma regla de la
   puerta 3 en cada paso de `.github/workflows/*.yml` y de las acciones
   locales `.github/actions/*/action.yml` cuyo `run:` lleve `curl` o
   `wget`: el paso declara en su `env:` un `<NOMBRE>_SHA256:` de 64 hex (o
   lo asigna con `=` en el propio `run:`), cada `curl` escribe con `-o` una
   ruta que comprueba un `sha256sum -c` del mismo `run:`, sin tuberías, sin
   `wget` y sin releases flotantes. El hallazgo cuenta desde la primera línea
   del paso. Un binario descargado sin hash en un job de `main` es código
   arbitrario con el token de caché de ese job (SECURITY.md T10). La puerta
   es léxica: un paso es un elemento de lista YAML y su `run:` el bloque
   (`|`/`>`) o la línea que sigue a la clave; las líneas comentadas del
   bloque se saltan.

Las recetas de instalación para usuarios de `docs/site/` quedan fuera: instalan
Rayito publicado, no una herramienta de esta construcción.

    python scripts/check_pins.py              # los caminos por defecto
    python scripts/check_pins.py FICHERO...   # otros caminos (tests)
"""

from __future__ import annotations

import re
import shlex
import sys
from pathlib import Path
from typing import NamedTuple

PINNED_ACTION = re.compile(r"[^@\s]+@[0-9a-f]{40}( +#.*)?")
USES_KEY = re.compile(r"^\s*(?:-\s+)?uses:\s*(?P<reference>\S.*?)\s*$")
UVX = "uvx"
LOCAL_ACTION_PREFIX = "./"
VERSION_PIN = "=="
FROM_FLAG = "--from"
WORKFLOW_SUFFIXES = frozenset({".yml", ".yaml"})
DOCKERFILE_NAME = "Dockerfile"
DEFAULT_PATHS = (
    ".github/workflows/*.yml",
    ".github/workflows/*.yaml",
    ".github/actions/*/action.yml",
    ".github/actions/*/action.yaml",
    ".github/release/requirements*.txt",
    "Makefile",
    "image/Dockerfile",
    "dev/local/*/Dockerfile",
    "kernel-sidecar/requirements*.txt",
)
ACTION_REASON = "la acción no está clavada a un SHA de 40 hex"
UVX_REASON = "la herramienta de uvx no lleva ==<versión>"
UVX_GRAPH_REASON = (
    "uvx resuelve al vuelo el grafo de esta herramienta: instálala desde un "
    "fichero de requisitos con --hash (.github/release/requirements-*.txt)"
)
DEPENDENCY_FREE_UVX_TOOLS = frozenset({"ruff"})
EXTRAS = re.compile(r"\[.*?\]")
DOWNLOAD_REASON = "la descarga no está verificada contra un sha256 fijado"
DOWNLOAD_TOOL = re.compile(r"\bcurl\b")
UNVERIFIABLE_TOOL = re.compile(r"\bwget\b")
REMOTE_ADD = re.compile(r"^ADD\s+(?:--\S+\s+)*[\"']?https?://", re.IGNORECASE)
CURL = "curl"
CURL_OUTPUT_FLAGS = frozenset({"-o", "--output"})
LONG_OUTPUT_PREFIX = "--output="
SHORT_OUTPUT_FLAG = "-o"
COMMAND_SEPARATORS = re.compile(r"&&|\|\||;")
PIPE = re.compile(r"(?<!\|)\|(?!\|)")
PINNED_SHA256 = re.compile(r"\b[A-Z][A-Z0-9_]*_SHA256=[0-9a-f]{64}\b")
PINNED_SHA256_ENV = re.compile(
    r"^\s*[A-Z][A-Z0-9_]*_SHA256:\s*[\"']?[0-9a-f]{64}[\"']?\s*$", re.MULTILINE
)
LIST_ITEM = re.compile(r"^(?P<indent>\s*)-\s+\S")
RUN_KEY = re.compile(r"^(?P<indent>\s*)(?:-\s+)?run:\s*(?P<value>.*?)\s*$")
BLOCK_SCALAR = re.compile(r"^[|>][+-]?\d*$")
SHELL_LINE_SEPARATOR = " ; "
SHA256_CHECK = "sha256sum -c"
FLOATING_RELEASE = re.compile(r"/releases/latest\b|/latest/download/")
LINE_CONTINUATION = "\\"
DNF_REASON = "el paquete dnf no lleva -<versión>-<release>"
PINNED_DNF_PACKAGES = frozenset({"git-core", "amazon-efs-utils"})
DNF = "dnf"
DNF_INSTALL = "install"
DOCKERFILE_RUN = "RUN"
#: Palabras de shell que pueden ir delante de una orden dentro de una capa
#: condicional (`RUN if ...; then dnf install ...; fi`, la capa de
#: `amazon-efs-utils` de `image/Dockerfile`): se quitan antes de mirar si la
#: orden es `dnf`.
SHELL_COMPOUND_KEYWORDS = frozenset({"then", "else", "do"})
SHELL_SEPARATORS = re.compile(r"&&|\|\||[;|]")
VERSION_START = re.compile(r"-(?=\d)")
PIP = "pip"
PIP_INSTALL = "install"
PIP_EXECUTABLE = re.compile(r"^pip(3(\.\d+)?)?$")
PYTHON_MODULE_PIP = re.compile(r"^python(3(\.\d+)?)?$")
MODULE_FLAG = "-m"
# Palabras que pueden preceder a un comando sin cambiar cuál es: palabras
# reservadas de la shell tras un `if ...;`/`for ...;`, envoltorios y
# asignaciones de entorno (`A=1 pip ...`, `env A=1 pip ...`).
COMMAND_PREFIXES = frozenset(
    {"then", "else", "do", "!", "sudo", "exec", "command", "nohup", "env", "time"}
)
ENV_ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
REQUIRE_HASHES_FLAG = "--require-hashes"
NO_DEPS_FLAG = "--no-deps"
ONLY_BINARY_PREFIX = "--only-binary"
PIP_FLAGS_REASON = (
    "el pip install no lleva --require-hashes, --no-deps y --only-binary=:all:"
)
REQUIREMENT_PIN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*==\S")
HASH_PIN = re.compile(r"--hash=sha256:[0-9a-f]{64}\b")
UNHASHED_PIN_REASON = "el pin no lleva --hash=sha256:"
REQUIREMENTS_FILE_PREFIX = "requirements"
REQUIREMENTS_FILE_SUFFIX = ".txt"

Finding = tuple[int, str, str]


class Instruction(NamedTuple):
    number: int
    first_line: str
    text: str


def is_comment(line: str) -> bool:
    return line.lstrip().startswith("#")


def action_reference(line: str) -> str | None:
    """La referencia de una línea cuya clave YAML es `uses:`, o `None`. La
    clave tiene que abrir la línea: `uses:` citado dentro del nombre de un
    paso o de una orden de shell no declara ninguna acción."""
    if is_comment(line):
        return None
    match = USES_KEY.match(line)
    return match.group("reference") if match else None


def unpinned_actions(text: str) -> list[Finding]:
    findings: list[Finding] = []
    for number, line in enumerate(text.splitlines(), start=1):
        reference = action_reference(line)
        if reference is None or reference.startswith(LOCAL_ACTION_PREFIX):
            continue
        if not PINNED_ACTION.fullmatch(reference):
            findings.append((number, line.strip(), ACTION_REASON))
    return findings


def split_words(fragment: str) -> list[str]:
    try:
        return shlex.split(fragment, posix=True)
    except ValueError:
        return fragment.split()


def tool_specification(arguments: list[str]) -> str | None:
    """La herramienta que `uvx` va a ejecutar: la de `--from` si está, si no la
    primera palabra que no sea una opción."""
    if FROM_FLAG in arguments:
        position = arguments.index(FROM_FLAG) + 1
        return arguments[position] if position < len(arguments) else None
    for argument in arguments:
        if not argument.startswith("-"):
            return argument
    return None


def uvx_invocations(line: str) -> list[list[str]]:
    if is_comment(line):
        return []
    words = split_words(line)
    return [words[index + 1 :] for index, word in enumerate(words) if word == UVX]


def tool_name(specification: str) -> str:
    """El nombre del paquete de una especificación (`ruff==0.16.7`,
    `rayito[mcp]==0.6.1`), sin versión ni extras y en minúsculas."""
    return EXTRAS.sub("", specification.split(VERSION_PIN, 1)[0]).strip().lower()


def unpinned_uvx(text: str) -> list[Finding]:
    findings: list[Finding] = []
    for number, line in enumerate(text.splitlines(), start=1):
        for arguments in uvx_invocations(line):
            specification = tool_specification(arguments)
            if specification is None or VERSION_PIN not in specification:
                findings.append((number, line.strip(), UVX_REASON))
            elif tool_name(specification) not in DEPENDENCY_FREE_UVX_TOOLS:
                findings.append((number, line.strip(), UVX_GRAPH_REASON))
    return findings


def dockerfile_instructions(text: str) -> list[Instruction]:
    """Las instrucciones lógicas de un `Dockerfile`: las líneas acabadas en
    `\\` se unen a la siguiente, como hace Docker, y las comentadas o vacías
    se saltan sin cortar la instrucción en curso (un comentario acabado en
    `\\` tampoco continúa nada)."""
    instructions: list[Instruction] = []
    pending: list[str] = []
    first_number, first_line = 0, ""
    for number, line in enumerate(text.splitlines(), start=1):
        if not line.strip() or is_comment(line):
            continue
        if not pending:
            first_number, first_line = number, line.strip()
        body = line.rstrip()
        continued = body.endswith(LINE_CONTINUATION)
        pending.append(body.removesuffix(LINE_CONTINUATION))
        if not continued:
            instructions.append(
                Instruction(first_number, first_line, " ".join(pending))
            )
            pending = []
    if pending:
        instructions.append(Instruction(first_number, first_line, " ".join(pending)))
    return instructions


def shell_commands(instruction: str) -> list[str]:
    """Las órdenes de shell de una instrucción, cortadas en `&&`, `||` y `;`
    (una tubería `|` queda dentro de su orden)."""
    return [command.strip() for command in COMMAND_SEPARATORS.split(instruction)]


def curl_output(command: str) -> str | None:
    """La ruta de `-o`/`--output` de una orden que empieza por `curl`, o
    `None` si la orden no empieza por `curl` (un `$(curl ...)`) o escribe a
    stdout."""
    words = split_words(command)
    if words[:1] == [DOCKERFILE_RUN]:
        words = words[1:]
    if words[:1] != [CURL]:
        return None
    for index, word in enumerate(words):
        if word in CURL_OUTPUT_FLAGS:
            return words[index + 1] if index + 1 < len(words) else None
        if word.startswith(LONG_OUTPUT_PREFIX):
            return word.removeprefix(LONG_OUTPUT_PREFIX)
        if word.startswith(SHORT_OUTPUT_FLAG) and len(word) > len(SHORT_OUTPUT_FLAG):
            return word.removeprefix(SHORT_OUTPUT_FLAG)
    return None


def checked_paths(commands: list[str]) -> set[str]:
    """Cada palabra (partida también por espacios dentro de las comillas) de
    las órdenes que llevan `sha256sum -c`: ahí aparece la ruta comprobada,
    sea en `echo "<sha>  <ruta>" | sha256sum -c -` o en un fichero de sumas."""
    paths: set[str] = set()
    for command in commands:
        if SHA256_CHECK in command:
            for word in split_words(command):
                paths.update(word.split())
    return paths


def download_is_verified(instruction: str, sha256_pinned: bool | None = None) -> bool:
    """`sha256_pinned` dice si el sha256 está fijado fuera del texto (el
    `env:` de un paso de workflow); sin él se busca `<NOMBRE>_SHA256=` en la
    propia instrucción, como en un `Dockerfile`."""
    pinned = (
        PINNED_SHA256.search(instruction) is not None
        if sha256_pinned is None
        else sha256_pinned or PINNED_SHA256.search(instruction) is not None
    )
    if (
        not pinned
        or SHA256_CHECK not in instruction
        or FLOATING_RELEASE.search(instruction) is not None
        or UNVERIFIABLE_TOOL.search(instruction) is not None
        or REMOTE_ADD.match(instruction) is not None
    ):
        return False
    commands = shell_commands(instruction)
    downloads = [command for command in commands if DOWNLOAD_TOOL.search(command)]
    checks = [command for command in commands if SHA256_CHECK in command]
    if len(downloads) > len(checks):
        return False
    verified = checked_paths(checks)
    for command in downloads:
        output = curl_output(command)
        if PIPE.search(command) or output is None or output not in verified:
            return False
    return True


def is_download(instruction: str) -> bool:
    return (
        DOWNLOAD_TOOL.search(instruction) is not None
        or UNVERIFIABLE_TOOL.search(instruction) is not None
        or REMOTE_ADD.match(instruction) is not None
    )


def unpinned_downloads(text: str) -> list[Finding]:
    return [
        (instruction.number, instruction.first_line, DOWNLOAD_REASON)
        for instruction in dockerfile_instructions(text)
        if is_download(instruction.text) and not download_is_verified(instruction.text)
    ]


def indentation(line: str) -> int:
    return len(line) - len(line.lstrip())


def run_script(step_lines: list[str]) -> str:
    """El `run:` de un paso como una sola instrucción de shell: las líneas
    del bloque se unen con `;` (cada una es una orden), las acabadas en `\\`
    con la siguiente, y las comentadas o vacías se saltan."""
    for position, line in enumerate(step_lines):
        match = RUN_KEY.match(line)
        if match is None or is_comment(line):
            continue
        value = match.group("value")
        if not BLOCK_SCALAR.match(value):
            return value
        key_indent = len(match.group("indent"))
        body: list[str] = []
        for following in step_lines[position + 1 :]:
            if following.strip() and indentation(following) <= key_indent:
                break
            body.append(following)
        commands: list[str] = []
        for instruction in dockerfile_instructions("\n".join(body)):
            commands.append(instruction.text.strip())
        return SHELL_LINE_SEPARATOR.join(commands)
    return ""


def workflow_steps(text: str) -> list[Instruction]:
    """Cada elemento de lista YAML de nivel más externo que lleve un `run:`,
    con su primera línea y su script (ver `run_script`). Un elemento acaba en
    la primera línea no vacía con su sangría o menos; los anidados se
    recorren como parte de su padre."""
    lines = text.splitlines()
    steps: list[Instruction] = []
    position = 0
    while position < len(lines):
        line = lines[position]
        match = LIST_ITEM.match(line)
        if match is None or is_comment(line):
            position += 1
            continue
        item_indent = len(match.group("indent"))
        end = position + 1
        while end < len(lines) and (
            not lines[end].strip() or indentation(lines[end]) > item_indent
        ):
            end += 1
        block = lines[position:end]
        script = run_script(block)
        if script:
            steps.append(Instruction(position + 1, line.strip(), "\n".join(block)))
        position = end
    return steps


def unpinned_workflow_downloads(text: str) -> list[Finding]:
    findings: list[Finding] = []
    for step in workflow_steps(text):
        script = run_script(step.text.splitlines())
        if not is_download(script):
            continue
        pinned = PINNED_SHA256_ENV.search(step.text) is not None
        if not download_is_verified(script, sha256_pinned=pinned):
            findings.append((step.number, step.first_line, DOWNLOAD_REASON))
    return findings


def dnf_install_packages(instruction: str) -> list[str]:
    """Los paquetes que nombran los `dnf install` de una instrucción: cada
    orden de shell (cortada en `&&`, `||`, `;` y `|`) cuya primera palabra,
    quitados `RUN` y un `then`/`else`/`do` de una capa condicional, es `dnf`
    y que lleva `install`. Las opciones (`-y`, `--setopt=...`) no son
    paquetes."""
    packages: list[str] = []
    for command in SHELL_SEPARATORS.split(instruction):
        words = split_words(command)
        if words[:1] == [DOCKERFILE_RUN]:
            words = words[1:]
        if words[:1] and words[0] in SHELL_COMPOUND_KEYWORDS:
            words = words[1:]
        if words[:1] != [DNF] or DNF_INSTALL not in words:
            continue
        arguments = words[words.index(DNF_INSTALL) + 1 :]
        packages.extend(word for word in arguments if not word.startswith("-"))
    return packages


def package_name(token: str) -> str:
    """El nombre de un paquete dnf: lo que va antes del primer `-<dígito>`."""
    return VERSION_START.split(token, maxsplit=1)[0]


def carries_version_and_release(token: str, name: str) -> bool:
    return re.fullmatch(rf"{re.escape(name)}-\d\S*-\S+", token) is not None


def unpinned_dnf_packages(text: str) -> list[Finding]:
    findings: list[Finding] = []
    for instruction in dockerfile_instructions(text):
        for token in dnf_install_packages(instruction.text):
            name = package_name(token)
            if name in PINNED_DNF_PACKAGES and not carries_version_and_release(
                token, name
            ):
                findings.append((instruction.number, token, DNF_REASON))
    return findings


def has_only_binary_all(words: list[str]) -> bool:
    """`--only-binary=:all:`, `--only-binary :all:` o `--only-binary all`."""
    for index, word in enumerate(words):
        if word == ONLY_BINARY_PREFIX:
            value = words[index + 1] if index + 1 < len(words) else ""
        elif word.startswith(ONLY_BINARY_PREFIX + "="):
            value = word.removeprefix(ONLY_BINARY_PREFIX + "=")
        else:
            continue
        if value.strip(":").lower() == "all":
            return True
    return False


def pip_installs(instruction: str) -> list[list[str]]:
    """Las palabras que siguen a cada `pip install`/`python3[.x] -m pip
    install` de una instrucción (cortada en `&&`, `||`, `;` y `|`), instale
    desde un fichero de requisitos o nombre paquetes sueltos."""
    commands: list[list[str]] = []
    for command in SHELL_SEPARATORS.split(instruction):
        words = split_words(command)
        if words[:1] == [DOCKERFILE_RUN]:
            words = words[1:]
        while words and (
            words[0] in COMMAND_PREFIXES or ENV_ASSIGNMENT.match(words[0])
        ):
            words = words[1:]
        program = words[0].rsplit("/", 1)[-1] if words else ""
        if (
            len(words) >= 3
            and PYTHON_MODULE_PIP.match(program)
            and words[1] == MODULE_FLAG
            and PIP_EXECUTABLE.match(words[2])
        ):
            words = words[3:]
        elif PIP_EXECUTABLE.match(program):
            words = words[1:]
        else:
            continue
        if words[:1] == [PIP_INSTALL]:
            commands.append(words[1:])
    return commands


def unhashed_pip_installs(text: str) -> list[Finding]:
    findings: list[Finding] = []
    for instruction in dockerfile_instructions(text):
        for arguments in pip_installs(instruction.text):
            if (
                REQUIRE_HASHES_FLAG not in arguments
                or NO_DEPS_FLAG not in arguments
                or not has_only_binary_all(arguments)
            ):
                findings.append(
                    (instruction.number, instruction.first_line, PIP_FLAGS_REASON)
                )
    return findings


def unhashed_requirement_pins(text: str) -> list[Finding]:
    """Cada línea de pin (`nombre==versión`, con sus continuaciones `\\` de
    `--hash=...` unidas) de un fichero de requisitos que no lleve ningún
    `--hash=sha256:` de 64 hex."""
    return [
        (line.number, line.first_line, UNHASHED_PIN_REASON)
        for line in dockerfile_instructions(text)
        if REQUIREMENT_PIN.match(line.text) and HASH_PIN.search(line.text) is None
    ]


def is_requirements_file(path: Path) -> bool:
    return (
        path.name.startswith(REQUIREMENTS_FILE_PREFIX)
        and path.suffix == REQUIREMENTS_FILE_SUFFIX
    )


def findings_for(path: Path) -> list[Finding]:
    text = path.read_text(encoding="utf-8")
    findings = set(unpinned_uvx(text))
    if path.suffix in WORKFLOW_SUFFIXES:
        findings.update(unpinned_actions(text))
        findings.update(unpinned_workflow_downloads(text))
    if path.name == DOCKERFILE_NAME:
        findings.update(unpinned_downloads(text))
        findings.update(unpinned_dnf_packages(text))
        findings.update(unhashed_pip_installs(text))
    if is_requirements_file(path):
        findings.update(unhashed_requirement_pins(text))
    return sorted(findings)


def resolve_paths(argv: list[str], root: Path) -> list[Path]:
    if argv:
        return [Path(argument) for argument in argv]
    paths: list[Path] = []
    for pattern in DEFAULT_PATHS:
        paths.extend(sorted(root.glob(pattern)))
    return paths


def displayed(path: Path, root: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return path.as_posix()


def main(argv: list[str], root: Path | None = None) -> int:
    base = root or Path.cwd()
    paths = [path for path in resolve_paths(argv, base) if path.is_file()]
    total = 0
    for path in paths:
        for number, line, reason in findings_for(path):
            total += 1
            print(f"KO {displayed(path, base)}:{number}: {reason}\n    {line}")
    if total:
        print(f"KO {total} invocación(es) sin clavar")
        return 1
    checked = ", ".join(displayed(path, base) for path in paths)
    print(
        f"OK {len(paths)} fichero(s): toda acción, todo uvx, toda descarga (Dockerfile y workflows), todo paquete dnf vigilado y todo pip install con hashes clavados ({checked})"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
