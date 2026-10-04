"""`DockerfileRenderer` (m15-templates): compila el `TemplateSpec` en texto,
inserta la capa antes del `CMD`/`ENTRYPOINT` final de la imagen base y deja
`rayd` como PID 1 (investigación §3.5)."""

from __future__ import annotations

import pytest

from rayito import Template
from rayito._templates._dockerfile import (
    LAYER_BEGIN_MARKER,
    compose_dockerfile,
    render_appended_layer,
)
from rayito.exceptions import BuildException

BASE_DOCKERFILE = 'FROM scratch\nCOPY rayd /usr/local/bin/rayd\nCMD ["/usr/local/bin/rayd"]\n'


def test_render_appended_layer_has_no_from_and_keeps_step_order() -> None:
    spec = Template().pip_install("pandas").copy("app/", "/srv/app/").spec
    rendered = render_appended_layer(spec)
    assert "FROM" not in rendered
    lines = [line for line in rendered.splitlines() if line and not line.startswith("#")]
    assert lines == [
        "RUN python3 -m pip install --no-cache-dir --break-system-packages pandas",
        'COPY ["__rayito_context/app/", "/srv/app/"]',
    ]


def test_compose_inserts_the_layer_before_the_final_cmd_and_repeats_it() -> None:
    spec = Template().pip_install("pandas").spec
    composed = compose_dockerfile(BASE_DOCKERFILE, spec)
    lines = composed.splitlines()
    assert lines[0] == "FROM scratch"
    assert "RUN python3 -m pip install --no-cache-dir --break-system-packages pandas" in lines
    assert lines[-2] == "USER root"
    assert lines[-1] == 'CMD ["/usr/local/bin/rayd"]'
    # rayd's own COPY still happens before the new layer.
    assert lines.index("COPY rayd /usr/local/bin/rayd") < lines.index(
        "RUN python3 -m pip install --no-cache-dir --break-system-packages pandas"
    )


def test_compose_adds_the_template_json_copy_only_when_start_is_set() -> None:
    without_start = compose_dockerfile(BASE_DOCKERFILE, Template().pip_install("pandas").spec)
    assert "__rayito_template.json" not in without_start

    with_start = compose_dockerfile(BASE_DOCKERFILE, Template().set_start_cmd("python app.py").spec)
    assert 'COPY ["__rayito_template.json", "/etc/rayito/template.json"]' in with_start


def test_compose_without_a_terminal_instruction_raises_build_exception() -> None:
    with pytest.raises(BuildException, match="CMD/ENTRYPOINT"):
        compose_dockerfile("FROM scratch\nRUN echo hi\n", Template().spec)


def test_rebuilding_an_already_composed_image_does_not_stack_layers() -> None:
    first = compose_dockerfile(BASE_DOCKERFILE, Template().pip_install("pandas").spec)
    second = compose_dockerfile(first, Template().copy("app/", "/srv/app/").spec)
    assert second.count(LAYER_BEGIN_MARKER) == 1
    assert "pip install" not in second
    assert 'COPY ["__rayito_context/app/", "/srv/app/"]' in second
