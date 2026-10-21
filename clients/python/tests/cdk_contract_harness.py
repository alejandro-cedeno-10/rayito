"""Arnés de Python del contrato `testdata/agentcore-cdk/` (ADR-032 D12.2):
construye el stack de un caso (`build_case`) y lo sintetiza
(`synthesize_case`) sin llamar a AWS. `scripts/gen_cdk_vectors.py` lo carga por
ruta para escribir los `expected.template.json` y `tests/unit/test_cdk_contract.py`
lo usa para comprobarlos. Necesita `rayito[cdk]` y Node (jsii).
"""

from __future__ import annotations

import importlib.util
import json
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Any, Final

import aws_cdk as cdk

from rayito.cdk import AgentImageBuild, MicrovmImageArtifact, RayitoAgent, RayitoMicrovmImage

REPO_ROOT: Final = Path(__file__).resolve().parents[3]
TESTDATA_DIR: Final = REPO_ROOT / "testdata"
CONTRACT_DIR: Final = TESTDATA_DIR / "agentcore-cdk"
CONTRACT: Final[dict[str, Any]] = json.loads(
    (CONTRACT_DIR / "contract.json").read_text(encoding="utf-8")
)
GENERATOR_PATH: Final = REPO_ROOT / "scripts" / "gen_cdk_vectors.py"
CASE_FORMAT: Final = CONTRACT["caseFormat"]
PROP_NAMES: Final[Mapping[str, str]] = CONTRACT["sdk"]["propNames"]
STACK_NAME: Final = CASE_FORMAT["stackName"]
APP_CONTEXT: Final[dict[str, Any]] = json.loads(CASE_FORMAT["appContext"])
OUTDIR_NAME: Final = "cdk.out"
FIXTURE_ZIP_NAME: Final = "rayito-image.zip"
CODE_BUILD: Final = "codeBuild"
LOCAL_DOCKER: Final = "localDocker"


def generator() -> ModuleType:
    """`scripts/gen_cdk_vectors.py` (sólo biblioteca estándar): la
    normalización de referencia y el zip de los fixtures."""
    spec = importlib.util.spec_from_file_location("gen_cdk_vectors", GENERATOR_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules.setdefault(spec.name, module)
    spec.loader.exec_module(module)
    return module


@dataclass(frozen=True)
class BuiltCase:
    app: cdk.App
    stack: cdk.Stack
    construct: RayitoAgent | RayitoMicrovmImage


def python_props(props: Mapping[str, Any]) -> dict[str, Any]:
    """Las props del caso (camelCase de TypeScript) con los nombres de Python
    (`contract.sdk.propNames`)."""
    return {PROP_NAMES[key]: value for key, value in props.items()}


def image_build(spec: Any) -> AgentImageBuild:
    """`"codeBuild"`, `{"codeBuild": {"timeoutMinutes": N}}` o `"localDocker"`."""
    if spec == LOCAL_DOCKER:
        return AgentImageBuild.local_docker()
    if isinstance(spec, Mapping):
        return AgentImageBuild.code_build(**python_props(spec[CODE_BUILD]))
    return AgentImageBuild.code_build()


def new_stack(case: Mapping[str, Any], scratch: Path) -> tuple[cdk.App, cdk.Stack]:
    """La `App` del contrato y el stack `RayitoCdkCase` (sin `env` si la
    región del caso es `null`)."""
    app = cdk.App(context=APP_CONTEXT, outdir=str(scratch / OUTDIR_NAME))
    environment = case["stack"]
    env = (
        None
        if environment["region"] is None
        else cdk.Environment(account=environment["account"], region=environment["region"])
    )
    return app, cdk.Stack(app, STACK_NAME, env=env)


def fixture_artifact(case: Mapping[str, Any], scratch: Path) -> MicrovmImageArtifact:
    spec = case["artifact"]
    destination = generator().write_fixture_zip(
        CONTRACT_DIR / spec["fixture"], scratch / FIXTURE_ZIP_NAME
    )
    return MicrovmImageArtifact.from_file(destination, sha256=spec.get("sha256"))


def build_case(case: Mapping[str, Any], scratch: Path) -> BuiltCase:
    """El stack de un caso con su constructo; lanza la excepción del caso si
    `expected` es un error."""
    app, stack = new_stack(case, scratch)
    props = python_props(case["props"])
    construct: RayitoAgent | RayitoMicrovmImage
    if case["construct"] == "RayitoAgent":
        construct = RayitoAgent(
            stack,
            case["id"],
            project=str(TESTDATA_DIR / case["project"]),
            build=image_build(case["build"]),
            **props,
        )
    else:
        construct = RayitoMicrovmImage(
            stack, case["id"], artifact=fixture_artifact(case, scratch), **props
        )
    return BuiltCase(app, stack, construct)


def nested_stacks(stack: cdk.Stack) -> list[cdk.NestedStack]:
    return [child for child in stack.node.find_all() if isinstance(child, cdk.NestedStack)]


def synthesize_case(case: Mapping[str, Any], scratch: Path) -> dict[str, Any]:
    """`{"root": plantilla, "nested": {logicalId: plantilla}}` del caso, sin
    normalizar."""
    built = build_case(case, scratch)
    assembly = built.app.synth()
    root = assembly.get_stack_by_name(STACK_NAME).template
    outdir = Path(assembly.directory)
    nested = {
        built.stack.resolve(child.nested_stack_resource.logical_id): json.loads(
            (outdir / child.template_file).read_text(encoding="utf-8")
        )
        for child in nested_stacks(built.stack)
        if child.nested_stack_resource is not None
    }
    return {"root": root, "nested": nested}


__all__ = ["BuiltCase", "build_case", "generator", "synthesize_case"]
