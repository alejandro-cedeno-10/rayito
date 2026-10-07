"""`AgentTemplate` y `rayito agent template build` (`ai-agent-fast-start`):
la receta con los pines de `limits.json`, el manifiesto, el demonio de
precarga, la validación y el contexto que llega a `Template.build`. Los
vectores de `testdata/agent/agent-template/cases.json` los comparte
`agent-template.test.ts`."""

from __future__ import annotations

import hashlib
import json
import subprocess
import time
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from rayito import AgentTemplate, AsyncAgentTemplate, Template
from rayito._agent import _template
from rayito._limits import (
    AGENT_DEEPAGENTS_REQUIREMENTS_SHA256,
    AGENT_OPENCODE_SHA256,
    AGENT_OPENCODE_VERSION,
    AGENT_RIPGREP_SHA256,
    AGENT_TEMPLATE_MANIFEST_SCHEMA,
)
from rayito._templates._dsl import AsyncTemplate
from rayito.exceptions import InvalidArgumentException

SENTINEL = object()
VECTORS = json.loads(
    (Path(__file__).parents[4] / "testdata" / "agent" / "agent-template" / "cases.json").read_text(
        encoding="utf-8"
    )
)["cases"]


def _from_options(options: dict[str, Any]) -> AgentTemplate:
    kwargs: dict[str, Any] = {k: tuple(v) if isinstance(v, list) else v for k, v in options.items()}
    return AgentTemplate(**kwargs)


@pytest.mark.parametrize("case", VECTORS, ids=[c["name"] for c in VECTORS])
def test_shared_vectors(case: dict[str, Any]) -> None:
    template = _from_options(case["options"])
    assert template.to_dockerfile() == case["dockerfile"]
    assert template.manifest() == case["manifest"]
    start = template.to_template().spec.start
    assert (start.start_cmd if start else None) == case["startCmd"]
    assert sorted(template.context_files()) == case["contextFiles"]


def test_recipe_pins_come_from_limits() -> None:
    dockerfile = AgentTemplate().to_dockerfile()
    assert f"v{AGENT_OPENCODE_VERSION}/opencode-linux-arm64.tar.gz" in dockerfile
    assert f"{AGENT_OPENCODE_SHA256}  /tmp/opencode.tar.gz" in dockerfile
    assert f"{AGENT_RIPGREP_SHA256}  /tmp/ripgrep.tar.gz" in dockerfile
    assert AGENT_DEEPAGENTS_REQUIREMENTS_SHA256 in dockerfile
    assert "--require-hashes --no-deps --only-binary=:all:" in dockerfile
    assert "chown -R root:root /opt/agents" in dockerfile
    assert 'ENV OPENCODE_DISABLE_CLAUDE_CODE="1"' in dockerfile


def test_requirements_package_data_matches_pinned_sha() -> None:
    data = _template.deepagents_requirements()
    assert hashlib.sha256(data).hexdigest() == AGENT_DEEPAGENTS_REQUIREMENTS_SHA256
    assert b"--hash=sha256:" in data


def test_manifest_schema_and_prefetch_paths() -> None:
    manifest = AgentTemplate().manifest()
    assert manifest["schema"] == AGENT_TEMPLATE_MANIFEST_SCHEMA
    assert manifest["opencode"] == {
        "version": AGENT_OPENCODE_VERSION,
        "sha256": AGENT_OPENCODE_SHA256,
    }
    assert manifest["prefetch_paths"] == ["/opt/agents/bin/opencode", "/opt/agents/bin/rg"]
    only = AgentTemplate(runtimes=("opencode",)).manifest()
    assert only["deepagents"] is None


def test_prefetch_off_has_no_start_cmd_nor_script() -> None:
    template = AgentTemplate(prefetch=False)
    assert template.to_template().spec.start is None
    assert _template.PREFETCH_SCRIPT_NAME not in template.context_files()


@pytest.mark.parametrize(
    "kwargs",
    [
        {"memory_mib": 1024},
        {"memory_mib": True},
        {"runtimes": ()},
        {"runtimes": ("claude",)},
        {"runtimes": ("opencode", "opencode")},
        {"name": ""},
    ],
)
def test_validation(kwargs: dict[str, Any]) -> None:
    with pytest.raises(InvalidArgumentException):
        AgentTemplate(**kwargs)


def test_build_passes_context_and_memory(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, Any] = {}

    def fake_build(template: Template, name: str, **kwargs: Any) -> object:
        context: Path = kwargs["context_dir"]
        seen.update(kwargs, name=name, files=sorted(p.name for p in context.iterdir()))
        seen["manifest"] = json.loads((context / "rayito-agent.json").read_text())
        return SENTINEL

    monkeypatch.setattr(Template, "build", staticmethod(fake_build))
    result: object = AgentTemplate(name="mi-agente", memory_mib=4096).build(
        bucket="amzn-s3-demo-bucket"
    )

    assert result is SENTINEL
    assert seen["name"] == "mi-agente"
    assert seen["memory_mb"] == 4096
    assert seen["bucket"] == "amzn-s3-demo-bucket"
    assert seen["files"] == [
        "deepagents_runner.py",
        "rayito-agent-prefetch",
        "rayito-agent.json",
        "requirements-deepagents.txt",
    ]
    assert seen["manifest"]["schema"] == AGENT_TEMPLATE_MANIFEST_SCHEMA
    assert not Path(seen["context_dir"]).exists()


async def test_async_build_uses_async_template(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, Any] = {}

    async def fake_build(template: Template, name: str, **kwargs: Any) -> object:
        seen["template"] = template
        seen["files"] = sorted(p.name for p in kwargs["context_dir"].iterdir())
        return SENTINEL

    monkeypatch.setattr(AsyncTemplate, "build", staticmethod(fake_build))
    result: object = await AsyncAgentTemplate(runtimes=("opencode",), prefetch=False).build(
        bucket="amzn-s3-demo-bucket"
    )

    assert result is SENTINEL
    assert isinstance(seen["template"], AsyncTemplate)
    assert seen["files"] == ["rayito-agent.json"]


def test_prefetch_script_detects_a_clock_jump(tmp_path: Path) -> None:
    """El demonio, con `date` falso: precarga sólo tras un salto de reloj."""
    script = tmp_path / "prefetch"
    script.write_bytes(_template.prefetch_script())
    script.chmod(0o755)
    target = tmp_path / "bin"
    target.write_bytes(b"x")
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"prefetch_paths": [str(target)]}))
    fakebin = tmp_path / "fakebin"
    fakebin.mkdir()
    counter = tmp_path / "count"
    counter.write_text("0")
    reads = tmp_path / "reads"
    (fakebin / "date").write_text(
        "#!/bin/bash\n"
        f'n=$(cat "{counter}"); echo $((n + 1)) > "{counter}"\n'
        'if [ "$n" -ge 2 ]; then echo $((1000 + n + 100)); else echo $((1000 + n)); fi\n'
    )
    (fakebin / "nice").write_text(
        f'#!/bin/bash\nshift 2\nif [ "$1" = cat ]; then echo "$3" >> "{reads}"; fi\n'
    )
    for tool in ("date", "nice"):
        (fakebin / tool).chmod(0o755)
    proc = subprocess.Popen(
        [str(script), str(manifest), "30", "0"],
        env={"PATH": f"{fakebin}:/usr/bin:/bin"},
    )
    try:
        deadline = time.monotonic() + 10
        while not reads.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
    finally:
        proc.kill()
        proc.wait()
    assert reads.read_text().splitlines()[0] == str(target)


def test_cli_maps_flags(monkeypatch: pytest.MonkeyPatch) -> None:
    from rayito.cli import agent as agent_cli

    built: list[AgentTemplate] = []

    class Info:
        template_id = "rayito-agent"
        build_id = "1"
        alias = None

    def fake_build(self: AgentTemplate, **kwargs: Any) -> Info:
        built.append(self)
        return Info()

    class Clients:
        region = "us-east-1"
        session = None

    monkeypatch.setattr(AgentTemplate, "build", fake_build)
    monkeypatch.setattr(agent_cli, "clients_of", lambda ctx: Clients())
    runner = CliRunner()
    result = runner.invoke(
        agent_cli.agent_app,
        [
            "template",
            "build",
            "--bucket",
            "amzn-s3-demo-bucket",
            "--no-deepagents",
            "--no-prefetch",
        ],
    )

    assert result.exit_code == 0, result.output
    assert built[0].runtimes == ("opencode",)
    assert built[0].prefetch is False
    assert "template_id=rayito-agent" in result.output


def test_cli_rejects_small_memory(monkeypatch: pytest.MonkeyPatch) -> None:
    from rayito.cli import agent as agent_cli

    monkeypatch.setattr(agent_cli, "clients_of", lambda ctx: object())
    result = CliRunner().invoke(
        agent_cli.agent_app,
        ["template", "build", "--bucket", "amzn-s3-demo-bucket", "--memory-mb", "1024"],
    )
    assert result.exit_code != 0


def test_deepagents_runner_is_installed_and_pinned_in_the_manifest() -> None:
    """El runner que `runtime="deepagents"` ejecuta se hornea de root y 0755
    en `DEEPAGENTS_RUNNER_PATH`, y su sha256 va en el manifiesto; sin
    deepagents, ni el fichero ni el hash."""
    template = AgentTemplate()
    runner = _template.deepagents_runner()
    assert template.context_files()[_template.DEEPAGENTS_RUNNER_NAME] == runner
    assert template.manifest()["runner_sha256"] == hashlib.sha256(runner).hexdigest()
    dockerfile = template.to_dockerfile()
    assert '"/opt/agents/rayito/deepagents_runner.py"]' in dockerfile
    assert "chmod 0755 /opt/agents/rayito/deepagents_runner.py" in dockerfile
    only = AgentTemplate(runtimes=("opencode",))
    assert only.manifest()["runner_sha256"] is None
    assert _template.DEEPAGENTS_RUNNER_NAME not in only.context_files()
