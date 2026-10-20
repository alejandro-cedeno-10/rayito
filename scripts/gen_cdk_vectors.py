"""Generate the golden templates of the CDK constructs (`agentcore-cdk`,
ADR-032) from `testdata/agentcore-cdk/cases/*/input.json`.

Every case whose `expected` is ``"template"`` is synthesized with the Python
construct (`rayito.cdk`, needs `rayito[cdk]` and Node for jsii), normalised
with `normalise_template` (the rules of `contract.json.normalisation`, which
the TypeScript tests apply too) and written to
``cases/<case>/expected.template.json``. Python and TypeScript must both
produce exactly that file: neither SDK is the reference, the file is.

Synthesis makes no AWS call: the stacks have a fixed environment with the
fictitious account 123456789012, the fixture zips are written to a temporary
directory, and the app context switches off path, asset and version
metadata.

Usage::

    uv run --project clients/python python scripts/gen_cdk_vectors.py
    uv run --project clients/python python scripts/gen_cdk_vectors.py --check

Until the Python construct and its harness exist (tasks 1.2 and 1.4 of the
OpenSpec change) the script reports the cases as pending: `--check` fails only when a golden file
exists that the current code no longer produces.
"""

from __future__ import annotations

import argparse
import difflib
import importlib.util
import json
import re
import sys
import tempfile
import zipfile
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
TESTDATA_DIR = REPO_ROOT / "testdata"
CONTRACT_DIR = TESTDATA_DIR / "agentcore-cdk"
CASES_DIR = CONTRACT_DIR / "cases"
CONTRACT_FILE = CONTRACT_DIR / "contract.json"
INPUT_FILE = "input.json"
EXPECTED_FILE = "expected.template.json"
EXPECTED_TEMPLATE = "template"

ASSET_HASH_PATTERN = re.compile(r"[0-9a-f]{64}")
ASSET_HASH_PLACEHOLDER = "<ASSET_HASH>"
BOOTSTRAP_QUALIFIER = "hnb659fds"
QUALIFIER_PLACEHOLDER = "<QUALIFIER>"
SDK_VERSION_TAG = "rayito:sdk-version"
SDK_VERSION_PLACEHOLDER = "<SDK_VERSION>"
REMOVED_SECTIONS: tuple[tuple[str, str], ...] = (
    ("Resources", "CDKMetadata"),
    ("Conditions", "CDKMetadataAvailable"),
    ("Parameters", "BootstrapVersion"),
    ("Rules", "CheckBootstrapVersion"),
)
NESTED_STACK_TYPE = "AWS::CloudFormation::Stack"

#: Fixture zips (contract.caseFormat.fields.artifact): sorted entries,
#: stored, fixed date and mode, so the bytes depend on content only.
ZIP_ENTRY_DATE_TIME = (1980, 1, 1, 0, 0, 0)
ZIP_ENTRY_MODE = 0o644

Synthesizer = Callable[[Mapping[str, Any], Path], dict[str, Any]]


def _without_sdk_version(value: Any) -> Any:
    if isinstance(value, list):
        return [_without_sdk_version(item) for item in value]
    if not isinstance(value, dict):
        return value
    if value.get("Key") == SDK_VERSION_TAG and "Value" in value:
        return {**value, "Value": SDK_VERSION_PLACEHOLDER}
    return {
        key: SDK_VERSION_PLACEHOLDER if key == SDK_VERSION_TAG else _without_sdk_version(item)
        for key, item in value.items()
    }


def _replace_strings(value: Any) -> Any:
    if isinstance(value, str):
        replaced = ASSET_HASH_PATTERN.sub(ASSET_HASH_PLACEHOLDER, value)
        return replaced.replace(BOOTSTRAP_QUALIFIER, QUALIFIER_PLACEHOLDER)
    if isinstance(value, list):
        return [_replace_strings(item) for item in value]
    if isinstance(value, dict):
        return {_replace_strings(key): _replace_strings(item) for key, item in value.items()}
    return value


def normalise_template(template: Mapping[str, Any]) -> dict[str, Any]:
    """The rules of `contract.json.normalisation`, in their order. Pure: the
    input is not modified."""
    result: dict[str, Any] = json.loads(json.dumps(template))
    for section, key in REMOVED_SECTIONS:
        entries = result.get(section)
        if isinstance(entries, dict):
            entries.pop(key, None)
            if not entries:
                result.pop(section)
    for resource in result.get("Resources", {}).values():
        if isinstance(resource, dict):
            resource.pop("Metadata", None)
    return _replace_strings(_without_sdk_version(result))


def render(document: Mapping[str, Any]) -> str:
    return json.dumps(document, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def write_fixture_zip(fixture: Path, destination: Path) -> Path:
    """The zip of a fixture directory, as `contract.caseFormat` describes."""
    files = sorted(path for path in fixture.rglob("*") if path.is_file())
    with zipfile.ZipFile(destination, "w", zipfile.ZIP_STORED) as archive:
        for path in files:
            info = zipfile.ZipInfo(path.relative_to(fixture).as_posix(), ZIP_ENTRY_DATE_TIME)
            info.external_attr = ZIP_ENTRY_MODE << 16
            archive.writestr(info, path.read_bytes())
    return destination


def load_cases() -> dict[str, dict[str, Any]]:
    return {
        path.parent.name: json.loads(path.read_text(encoding="utf-8"))
        for path in sorted(CASES_DIR.glob(f"*/{INPUT_FILE}"))
    }


#: The Python side of the contract harness (written with the construct,
#: task 1.4): ``synthesize_case(case, scratch) -> {"root": template,
#: "nested": {logicalId: template}}``. It lives with the tests, not in the
#: wheel, and the unit tests use the same function.
PYTHON_HARNESS = REPO_ROOT / "clients" / "python" / "tests" / "cdk_contract_harness.py"
HARNESS_FUNCTION = "synthesize_case"


def python_synthesizer() -> Synthesizer | None:
    """`synthesize_case` of the Python harness, or `None` while the harness
    (or the `cdk` extra) does not exist yet."""
    if not PYTHON_HARNESS.is_file() or importlib.util.find_spec("aws_cdk") is None:
        return None
    spec = importlib.util.spec_from_file_location("cdk_contract_harness", PYTHON_HARNESS)
    if spec is None or spec.loader is None:
        return None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    synthesize: Synthesizer = getattr(module, HARNESS_FUNCTION)
    return synthesize


def expected_documents(synthesize: Synthesizer) -> dict[Path, str]:
    outputs: dict[Path, str] = {}
    for name, case in load_cases().items():
        if case.get("expected") != EXPECTED_TEMPLATE:
            continue
        with tempfile.TemporaryDirectory(prefix="rayito-cdk-case-") as scratch:
            synthesized = synthesize(case, Path(scratch))
        document = {
            "root": normalise_template(synthesized["root"]),
            "nested": {
                logical_id: normalise_template(template)
                for logical_id, template in sorted(synthesized.get("nested", {}).items())
            },
        }
        outputs[CASES_DIR / name / EXPECTED_FILE] = render(document)
    return outputs


def check(outputs: Mapping[Path, str]) -> int:
    drifted = 0
    for path, expected in outputs.items():
        actual = path.read_text(encoding="utf-8") if path.exists() else ""
        if actual == expected:
            continue
        drifted += 1
        relative = path.relative_to(REPO_ROOT).as_posix()
        sys.stdout.write(f"gen_cdk_vectors: {relative} drifts from the construct\n")
        sys.stdout.writelines(
            difflib.unified_diff(
                actual.splitlines(keepends=True),
                expected.splitlines(keepends=True),
                fromfile=f"{relative} (on disk)",
                tofile=f"{relative} (synthesized)",
            )
        )
    if drifted:
        sys.stdout.write("gen_cdk_vectors: run `scripts/gen_cdk_vectors.py` to regenerate\n")
    return 1 if drifted else 0


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true", help="exit 1 on drift")
    args = parser.parse_args(argv)
    synthesize = python_synthesizer()
    if synthesize is None:
        goldens = sorted(CASES_DIR.glob(f"*/{EXPECTED_FILE}"))
        if goldens:
            sys.stdout.write(
                "gen_cdk_vectors: the Python harness or rayito[cdk] is missing but golden "
                "templates exist\n"
            )
            return 1
        sys.stdout.write(
            f"gen_cdk_vectors: no Python harness yet; {len(load_cases())} cases pending\n"
        )
        return 0
    outputs = expected_documents(synthesize)
    if args.check:
        return check(outputs)
    for path, text in outputs.items():
        path.write_text(text, encoding="utf-8")
        sys.stdout.write(f"gen_cdk_vectors: wrote {path.relative_to(REPO_ROOT).as_posix()}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
