"""Render the `OptionalStack` templates (and, when a component has Lambda
code, their artifact zip) into both SDKs (M15 foundations, ADR-016).

Single source: `infra/<component>.yaml`, plus `infra/lambdas/<component>/`
when the component bundles Lambda code. Output, deterministic and per
component (a feature adding its own `infra/<component>.yaml` needs no edit
here — this script discovers every CloudFormation template under `infra/`
except the two that predate the `OptionalStack` convention and are deployed
by hand, per `infra/README.md`). `infra/iam.yaml` and
`infra/artifact-bucket.yaml` are rendered too: they are not `OptionalStack`
catalog components, but `rayito setup` deploys them as the `rayito-m0-iam`
and `rayito-m0-artifacts` stacks (ADR-030):

- ``clients/python/src/rayito/_stacks/_templates/<component>.yaml`` (the
  template, copied verbatim)
- ``clients/python/src/rayito/_stacks/_artifacts/<component>.zip`` (only
  when the component has Lambda code)
- ``clients/typescript/src/stacks/templates/<component>.gen.ts`` (the
  template string plus the artifact, base64, or ``undefined``)

Usage::

    python scripts/gen_stack_assets.py           # render everything
    python scripts/gen_stack_assets.py --check   # exit 1 on drift
"""

from __future__ import annotations

import ast
import base64
import difflib
import io
import json
import sys
import zipfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
INFRA_DIR = REPO_ROOT / "infra"
PYTHON_TEMPLATES_DIR = (
    REPO_ROOT / "clients" / "python" / "src" / "rayito" / "_stacks" / "_templates"
)
PYTHON_ARTIFACTS_DIR = (
    REPO_ROOT / "clients" / "python" / "src" / "rayito" / "_stacks" / "_artifacts"
)
TYPESCRIPT_TEMPLATES_DIR = (
    REPO_ROOT / "clients" / "typescript" / "src" / "stacks" / "templates"
)

#: Pre-date the `OptionalStack` convention (ADR-016) and are deployed by
#: hand (`infra/README.md`), not through `rayito stack`/`OptionalStacks`.
#: `iam` and `artifact-bucket` are not listed: `rayito setup` deploys them
#: (ADR-030).
EXCLUDED_TEMPLATES = frozenset({"egress-connector", "ci-oidc-role"})


def discover_components() -> list[str]:
    return sorted(
        path.stem for path in INFRA_DIR.glob("*.yaml") if path.stem not in EXCLUDED_TEMPLATES
    )


def lambda_dir(component: str) -> Path:
    return INFRA_DIR / "lambdas" / component.replace("-", "_")


#: Only Lambda source is shipped: `.py` files, outside the component's own
#: `tests/` and outside any hidden directory (tool caches such as
#: `.mypy_cache`/`.pytest_cache`) or `__pycache__` (bytecode is not
#: deterministic: it embeds the interpreter version and the absolute source
#: path). An allowlist, so a dirty working tree can never leak into the zip.
ARTIFACT_SOURCE_SUFFIXES = frozenset({".py"})
EXCLUDED_ARTIFACT_DIR_NAMES = frozenset({"__pycache__", "tests"})
HIDDEN_PREFIX = "."


def _is_artifact_source(path: Path, source: Path) -> bool:
    if path.suffix not in ARTIFACT_SOURCE_SUFFIXES:
        return False
    directories = path.relative_to(source).parts[:-1]
    return not any(
        part in EXCLUDED_ARTIFACT_DIR_NAMES or part.startswith(HIDDEN_PREFIX)
        for part in directories
    )


#: botocore service models a component's Lambdas need but the Lambda
#: runtime's own `boto3` does not ship (decision 8 of the M15 architecture),
#: as `(service name for boto3.client(...), model file)`. The model stays in
#: `docs/aws-api/` (its single source); it is injected into the zip at
#: generation time under ``models/<service>/<apiVersion>/service-2.json``,
#: the layout botocore's loader expects below `AWS_DATA_PATH`.
BUNDLED_SERVICE_MODELS: dict[str, tuple[tuple[str, Path], ...]] = {
    "events-webhooks": (("lambda-microvms", REPO_ROOT / "docs" / "aws-api" / "service-2.json"),),
    "dashboard": (("lambda-microvms", REPO_ROOT / "docs" / "aws-api" / "service-2.json"),),
    "quota-alerts": (("lambda-microvms", REPO_ROOT / "docs" / "aws-api" / "service-2.json"),),
}
MODELS_DIR_NAME = "models"
SERVICE_MODEL_FILE_NAME = "service-2.json"
#: botocore reads the paginator definitions from a sibling file of the model.
#: Without it the runtime's `boto3` knows the operations of a bundled model
#: but `get_paginator` raises `OperationNotPageableError` (real AWS, 2026-10-13:
#: every list call of the `dashboard` Lambda answered `internal_error`).
SERVICE_PAGINATORS_FILE_NAME = "paginators-1.json"

#: Components whose Lambda imports the Python SDK itself (the `dashboard`
#: stack serves the same API and services as `rayito dashboard`, and the
#: `quota-alerts` check reuses its `Quotas` service). Their zip
#: carries the SDK modules the Lambda sources import, transitively, copied
#: verbatim from `clients/python/src/rayito/`; every package `__init__.py`
#: is replaced by a docstring-only stub, so importing `rayito.x` never
#: drags in the SDK's whole public surface (grpc, the proto code, the CLI).
#: `tests/unit/test_dashboard_stack_bundle.py`-style checks import the
#: handler from the extracted zip to prove the closure is complete.
VENDORED_SDK_COMPONENTS = frozenset({"dashboard", "quota-alerts"})
SDK_PACKAGE = "rayito"
PYTHON_SDK_SOURCE = REPO_ROOT / "clients" / "python" / "src"
PACKAGE_INIT = "__init__.py"
PACKAGE_STUB = (
    b'"""Subset of the rayito SDK bundled into a stack Lambda '
    b'(generated by scripts/gen_stack_assets.py)."""\n'
)
#: Static files of the SDK a vendored component serves, by directory (inside
#: `PYTHON_SDK_SOURCE`) and suffix: the dashboard's SPA.
VENDORED_DATA_DIRS: dict[str, tuple[str, ...]] = {"dashboard": ("rayito/_dashboard/_static",)}
VENDORED_DATA_SUFFIXES = frozenset({".html", ".css", ".js", ".svg"})

#: Fixed so the zip is byte-for-byte reproducible (`--check`).
ZIP_ENTRY_DATE_TIME = (1980, 1, 1, 0, 0, 0)
ZIP_ENTRY_MODE = 0o644


def bundled_model_entries(component: str) -> dict[str, bytes]:
    """`{arcname: bytes}` for every model `BUNDLED_SERVICE_MODELS` lists
    for `component`, with the paginators file that sits next to it (empty
    for the other components)."""
    entries: dict[str, bytes] = {}
    for service_name, model_path in BUNDLED_SERVICE_MODELS.get(component, ()):
        raw = model_path.read_bytes()
        api_version = json.loads(raw)["metadata"]["apiVersion"]
        model_dir = f"{MODELS_DIR_NAME}/{service_name}/{api_version}"
        entries[f"{model_dir}/{SERVICE_MODEL_FILE_NAME}"] = raw
        paginators = model_path.with_name(SERVICE_PAGINATORS_FILE_NAME)
        if paginators.is_file():
            entries[f"{model_dir}/{SERVICE_PAGINATORS_FILE_NAME}"] = paginators.read_bytes()
    return entries


def sdk_module_path(module: str) -> Path | None:
    """The source file of an SDK module (`rayito.a.b` -> `a/b.py` or the
    package's `__init__.py`), or `None` if `module` is not in the SDK."""
    base = PYTHON_SDK_SOURCE.joinpath(*module.split("."))
    if base.with_suffix(".py").is_file():
        return base.with_suffix(".py")
    init = base / PACKAGE_INIT
    return init if init.is_file() else None


def is_type_checking_guard(node: ast.AST) -> bool:
    if not isinstance(node, ast.If):
        return False
    test = node.test
    return (isinstance(test, ast.Name) and test.id == "TYPE_CHECKING") or (
        isinstance(test, ast.Attribute) and test.attr == "TYPE_CHECKING"
    )


def runtime_import_nodes(tree: ast.AST) -> list[ast.Import | ast.ImportFrom]:
    """The imports that run, skipping `if TYPE_CHECKING:` bodies (with
    `from __future__ import annotations` they never execute, and they are
    where the SDK imports its heaviest types)."""
    found: list[ast.Import | ast.ImportFrom] = []
    for child in ast.iter_child_nodes(tree):
        if is_type_checking_guard(child):
            continue
        if isinstance(child, (ast.Import, ast.ImportFrom)):
            found.append(child)
        found.extend(runtime_import_nodes(child))
    return found


def imported_sdk_modules(source: bytes) -> set[str]:
    """Every `rayito...` module (and imported submodule) a source imports."""
    found: set[str] = set()
    for node in runtime_import_nodes(ast.parse(source)):
        if isinstance(node, ast.Import):
            names = [alias.name for alias in node.names]
        elif node.module and node.level == 0:
            names = [node.module, *(f"{node.module}.{alias.name}" for alias in node.names)]
        else:
            continue
        found.update(
            name for name in names if name == SDK_PACKAGE or name.startswith(f"{SDK_PACKAGE}.")
        )
    return found


def vendored_sdk_entries(sources: dict[str, bytes]) -> dict[str, bytes]:
    """`{arcname: bytes}` of the SDK modules `sources` import, transitively,
    plus a stub `__init__.py` for every package on the way."""
    entries: dict[str, bytes] = {}
    pending = {name for source in sources.values() for name in imported_sdk_modules(source)}
    seen: set[str] = set()
    while pending:
        module = pending.pop()
        if module in seen:
            continue
        seen.add(module)
        path = sdk_module_path(module)
        if path is None:
            continue
        parts = module.split(".")
        is_package = path.name == PACKAGE_INIT
        stubbed = len(parts) if is_package else len(parts) - 1
        for depth in range(1, stubbed + 1):
            entries["/".join(parts[:depth]) + "/" + PACKAGE_INIT] = PACKAGE_STUB
        if is_package:
            continue
        entries["/".join(parts) + ".py"] = path.read_bytes()
        pending |= imported_sdk_modules(path.read_bytes())
    return entries


def vendored_data_entries(component: str) -> dict[str, bytes]:
    entries: dict[str, bytes] = {}
    for directory in VENDORED_DATA_DIRS.get(component, ()):
        root = PYTHON_SDK_SOURCE / directory
        for path in root.rglob("*"):
            if path.is_file() and path.suffix in VENDORED_DATA_SUFFIXES:
                entries[path.relative_to(PYTHON_SDK_SOURCE).as_posix()] = path.read_bytes()
    return entries


def build_artifact(component: str) -> bytes | None:
    """A deterministic zip (fixed mtimes, sorted entries) of the
    component's Lambda source plus its bundled service models, or `None`
    when it has no Lambda source (`_is_artifact_source` decides what
    counts)."""
    source = lambda_dir(component)
    if not source.is_dir():
        return None
    entries = {
        path.relative_to(source).as_posix(): path.read_bytes()
        for path in source.rglob("*")
        if path.is_file() and _is_artifact_source(path, source)
    }
    if not entries:
        return None
    if component in VENDORED_SDK_COMPONENTS:
        entries.update(vendored_sdk_entries(entries))
        entries.update(vendored_data_entries(component))
    entries.update(bundled_model_entries(component))
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for arcname in sorted(entries):
            info = zipfile.ZipInfo(arcname, date_time=ZIP_ENTRY_DATE_TIME)
            info.external_attr = ZIP_ENTRY_MODE << 16
            archive.writestr(info, entries[arcname])
    return buffer.getvalue()


def ts_string_literal(text: str) -> str:
    escaped = text.replace("\\", "\\\\").replace("`", "\\`").replace("${", "\\${")
    return f"`{escaped}`"


#: biome.json's `formatter.lineWidth` for the TypeScript client; matched
#: here so a generated `.gen.ts` never needs `biome check --write` to pass
#: `pnpm lint`, whether or not the component has an artifact.
BIOME_LINE_WIDTH = 100


def ts_assignment(prefix: str, literal: str) -> str:
    """`prefix` is everything up to and including the ` =` (name and type
    annotation); `literal` is the string/template literal. A single string
    token can't be wrapped further by a formatter, so this only ever
    chooses between "value on the same line" and "value on its own
    line" — whichever biome's own formatter would also choose, measured on
    the *first physical line* (a template literal's own embedded newlines
    already end that line early, same as biome sees it)."""
    first_line = f"{prefix} {literal}".split("\n", 1)[0]
    if len(first_line) <= BIOME_LINE_WIDTH:
        return f"{prefix} {literal};\n"
    return f"{prefix}\n  {literal};\n"


def render_typescript(component: str, template_text: str, artifact: bytes | None) -> str:
    artifact_literal = "undefined" if artifact is None else f'"{base64.b64encode(artifact).decode("ascii")}"'
    return (
        f"// GENERATED by scripts/gen_stack_assets.py from infra/{component}.yaml.\n"
        "// Do not edit by hand: run `python scripts/gen_stack_assets.py`.\n"
        + ts_assignment("export const TEMPLATE_BODY =", ts_string_literal(template_text))
        + "// Base64 of the component's Lambda source zip, or undefined when it has none.\n"
        + ts_assignment("export const ARTIFACT_BASE64: string | undefined =", artifact_literal)
    )


def renders() -> dict[Path, bytes]:
    outputs: dict[Path, bytes] = {}
    for component in discover_components():
        template_text = (INFRA_DIR / f"{component}.yaml").read_text(encoding="utf-8")
        artifact = build_artifact(component)
        outputs[PYTHON_TEMPLATES_DIR / f"{component}.yaml"] = template_text.encode("utf-8")
        if artifact is not None:
            outputs[PYTHON_ARTIFACTS_DIR / f"{component}.zip"] = artifact
        outputs[TYPESCRIPT_TEMPLATES_DIR / f"{component}.gen.ts"] = render_typescript(
            component, template_text, artifact
        ).encode("utf-8")
    return outputs


def current_bytes(path: Path) -> bytes:
    return path.read_bytes() if path.exists() else b""


def write_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def check(outputs: dict[Path, bytes]) -> int:
    drifted = 0
    for path, expected in outputs.items():
        actual = current_bytes(path)
        if actual == expected:
            continue
        drifted += 1
        relative = path.relative_to(REPO_ROOT).as_posix()
        sys.stdout.write(f"gen_stack_assets: {relative} drifts from infra/\n")
        if path.suffix in (".yaml", ".ts"):
            diff = difflib.unified_diff(
                actual.decode("utf-8", "replace").splitlines(keepends=True),
                expected.decode("utf-8", "replace").splitlines(keepends=True),
                fromfile=f"{relative} (on disk)",
                tofile=f"{relative} (from infra/)",
            )
            sys.stdout.writelines(diff)
    if drifted:
        sys.stdout.write("gen_stack_assets: run `python scripts/gen_stack_assets.py` to regenerate\n")
    return 1 if drifted else 0


def main(argv: list[str]) -> int:
    outputs = renders()
    if "--check" in argv:
        return check(outputs)
    for path, data in outputs.items():
        write_bytes(path, data)
        sys.stdout.write(f"gen_stack_assets: wrote {path.relative_to(REPO_ROOT).as_posix()}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
