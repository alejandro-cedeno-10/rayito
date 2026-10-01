"""`DockerfileRenderer` (dominio puro, investigación §3.5): compila las
instrucciones de cable de un `TemplateSpec` en texto Dockerfile. Dos
funciones:

- `render_appended_layer` renderiza sólo la capa que añade este `Template`
  (sin `FROM`: la imagen base no se conoce aquí, sólo su referencia) — es lo
  que devuelve `Template.to_dockerfile()` como vista previa.
- `compose_dockerfile` hace la composición real: inserta esa capa en el
  Dockerfile de la imagen base **antes** de su última `CMD`/`ENTRYPOINT`, y
  cierra con `USER root` más esa misma `CMD`/`ENTRYPOINT` repetida, para que
  `rayd` siga siendo PID 1 (investigación §3.5). Recibe el texto de la
  imagen base como argumento (lo trae `_build.py` de `codeArtifact`, un
  `s3:GetObject`): esta función no hace ninguna E/S.

Nada aquí sabe lo que es un build de AWS: sólo transforma texto.
"""

from __future__ import annotations

from typing import Final

from rayito._templates._instructions import (
    CopyStep,
    EnvStep,
    RunStep,
    TemplateSpec,
    UserStep,
    WorkdirStep,
)
from rayito.exceptions import BuildException

#: Marca el principio/fin de la capa que `Template` añade, para que un
#: `compose_dockerfile` posterior sobre el mismo texto (reconstruir desde
#: una imagen ya compuesta) la reemplace en vez de duplicarla.
LAYER_BEGIN_MARKER: Final = "# >>> rayito template layer (m15-templates), no editar a mano"
LAYER_END_MARKER: Final = "# <<< rayito template layer"

#: Instrucciones de imagen que marcan dónde "termina" el Dockerfile base
#: (todo lo que viene después de la última de éstas es lo que hay que
#: conservar y repetir al final, para que `rayd` siga siendo PID 1).
_TERMINAL_KEYWORDS: Final = ("CMD", "ENTRYPOINT")

#: Ruta del `StartSpec` serializado dentro del contexto de build (la
#: escribe `_artifact.py` junto al resto de ficheros del zip) y ruta a la
#: que esa `COPY` lo deja en la imagen: `rayd` sólo mira esta última
#: (`rayito.template/1`, investigación §3.5).
TEMPLATE_JSON_CONTEXT_PATH: Final = "__rayito_template.json"
TEMPLATE_JSON_IMAGE_PATH: Final = "/etc/rayito/template.json"


def _render_step(step: object) -> str:
    if isinstance(step, CopyStep):
        return f"COPY {step.src} {step.dst}"
    if isinstance(step, EnvStep):
        return f'ENV {step.key}="{step.value}"'
    if isinstance(step, RunStep):
        return f"RUN {step.cmd}"
    if isinstance(step, WorkdirStep):
        return f"WORKDIR {step.path}"
    if isinstance(step, UserStep):
        return f"USER {step.user}"
    raise TypeError(f"instrucción de cable desconocida: {step!r}")  # pragma: no cover - exhaustivo


def render_appended_layer(spec: TemplateSpec) -> str:
    """La capa que este `Template` añade, en orden, como texto Dockerfile.
    No incluye `FROM`: eso depende de la imagen base, resuelta sólo al
    construir (`Template.build()`). Si `spec.start` está puesto, añade al
    final la `COPY` que deja `/etc/rayito/template.json` en la imagen
    (`_artifact.py` escribe ese fichero en el contexto)."""
    lines = [LAYER_BEGIN_MARKER, *(_render_step(step) for step in spec.steps)]
    if spec.start is not None:
        lines.append(f"COPY {TEMPLATE_JSON_CONTEXT_PATH} {TEMPLATE_JSON_IMAGE_PATH}")
    lines.append(LAYER_END_MARKER)
    return "\n".join(lines) + "\n"


def _split_terminal_instruction(base_dockerfile: str) -> tuple[str, str]:
    """`(cabeza, última_instrucción_terminal)`: todo antes de la última
    `CMD`/`ENTRYPOINT` de nivel superior, y esa línea tal cual. Cada imagen
    de `rayito image publish` termina en exactamente una de las dos
    (`cli/_publish.py` genera el Dockerfile base); si no hay ninguna, el
    build no puede garantizar que `rayd` siga siendo PID 1 y se rechaza
    antes de subir nada."""
    lines = base_dockerfile.splitlines()
    for index in range(len(lines) - 1, -1, -1):
        stripped = lines[index].strip()
        if any(stripped.startswith(keyword) for keyword in _TERMINAL_KEYWORDS):
            head = "\n".join(lines[:index])
            return (head + "\n" if head else ""), lines[index]
    raise BuildException(
        "la imagen base no tiene una instrucción CMD/ENTRYPOINT final: Template.build() no "
        "puede garantizar que rayd siga siendo PID 1",
        reason="base_image_missing_entrypoint",
    )


def compose_dockerfile(base_dockerfile: str, spec: TemplateSpec) -> str:
    """El Dockerfile completo que sube `Template.build()`: la imagen base,
    con la capa de `spec` insertada justo antes de su `CMD`/`ENTRYPOINT`
    final, cerrada con `USER root` y esa misma instrucción terminal
    repetida."""
    if LAYER_BEGIN_MARKER in base_dockerfile:
        head, _ = base_dockerfile.split(LAYER_BEGIN_MARKER, 1)
        _, terminal = _split_terminal_instruction(base_dockerfile)
    else:
        head, terminal = _split_terminal_instruction(base_dockerfile)
    layer = render_appended_layer(spec)
    return f"{head}{layer}USER root\n{terminal}\n"
