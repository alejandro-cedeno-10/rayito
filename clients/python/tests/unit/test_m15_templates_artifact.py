"""`ArtifactAssembler` (m15-templates): el zip final junta todo el zip base
(un `RUN`/`COPY` de su propio Dockerfile puede necesitar cualquiera de sus
ficheros), con el Dockerfile sustituido por la composición y
`template.json` añadido sólo si hace falta; determinista."""

from __future__ import annotations

from rayito import Template
from rayito._templates._artifact import assemble_artifact, read_zip_entries, start_spec_to_json
from rayito._templates._dockerfile import TEMPLATE_JSON_CONTEXT_PATH

from .fake_templates import make_base_zip

BASE_DOCKERFILE = 'FROM scratch\nCOPY rayd /usr/local/bin/rayd\nCMD ["/usr/local/bin/rayd"]\n'


def test_assemble_artifact_keeps_every_base_entry_and_replaces_the_dockerfile() -> None:
    base_zip = make_base_zip(BASE_DOCKERFILE, {"rayd": b"\x7fELF..."})
    spec = Template().pip_install("pandas").spec
    artifact = assemble_artifact(base_zip, spec, ())
    entries = read_zip_entries(artifact)
    assert entries["rayd"] == b"\x7fELF..."
    dockerfile = entries["Dockerfile"].decode()
    assert "RUN python3 -m pip install --no-cache-dir --break-system-packages pandas" in dockerfile


def test_assemble_artifact_adds_context_files_and_template_json_when_start_is_set() -> None:
    base_zip = make_base_zip(BASE_DOCKERFILE)
    spec = Template().copy("app/main.py", "/srv/main.py").set_start_cmd("python /srv/main.py").spec
    artifact = assemble_artifact(base_zip, spec, [("app/main.py", b"print(1)")])
    entries = read_zip_entries(artifact)
    assert entries["__rayito_context/app/main.py"] == b"print(1)"
    assert TEMPLATE_JSON_CONTEXT_PATH in entries


def test_assemble_artifact_is_deterministic_for_the_same_inputs() -> None:
    base_zip = make_base_zip(BASE_DOCKERFILE)
    spec = Template().pip_install("pandas").spec
    first = assemble_artifact(base_zip, spec, ())
    second = assemble_artifact(base_zip, spec, ())
    assert first == second


def test_start_spec_to_json_round_trips_the_essential_fields() -> None:
    spec = Template().set_start_cmd("run.sh", "test -e /tmp/ready", envs={"A": "1"}).spec
    import json

    assert spec.start is not None
    payload = json.loads(start_spec_to_json(spec.start))
    assert payload["start_cmd"] == "run.sh"
    assert payload["ready_cmd"] == "test -e /tmp/ready"
    assert payload["envs"] == {"A": "1"}


def test_a_context_file_named_like_a_base_entry_never_replaces_it() -> None:
    """T26: a project `Dockerfile` or `rayd` copied with `.copy(".", ...)`
    lands under the reserved context prefix, never over the composed
    Dockerfile or the agent binary of the base zip."""
    base_zip = make_base_zip(BASE_DOCKERFILE, {"rayd": b"\x7fELF"})
    spec = Template().copy(".", "/srv/app/").spec
    artifact = assemble_artifact(
        base_zip, spec, [("Dockerfile", b"FROM evil"), ("rayd", b"not the agent")]
    )
    entries = read_zip_entries(artifact)
    assert entries["rayd"] == b"\x7fELF"
    assert entries["Dockerfile"].decode().startswith("FROM scratch")
    assert entries["__rayito_context/Dockerfile"] == b"FROM evil"
    assert entries["__rayito_context/rayd"] == b"not the agent"
