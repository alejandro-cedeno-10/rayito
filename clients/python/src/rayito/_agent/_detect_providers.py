"""Proveedores de modelo de un proyecto (ADR-029): por sus dependencias, por
los nombres de variable de entorno que el código, el README o un
`.env.example` mencionan y por la configuración de Hermes u OpenCode. Cada
proveedor es un preset de pasarela existente (`bedrock_gateway`,
`openai_gateway`...). Sólo se leen nombres: los valores de un `.env` nunca se
leen ni se guardan, y los literales de modelo son identificadores públicos.

Sin literal de modelo, el código puede elegirlo por su configuración
(`modelFromCode`, ADR-033 D2): una URL base propia en el constructor del
cliente convierte `openai` en `openai-compatible` (con un literal de modelo,
sólo con `--model-base-url`), y los nombres de las variables del id, la URL
base y la clave se guardan en `model_envs`.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Final

from rayito._agent._detect_report import DetectionEvidence, DetectionProvider, DetectOverrides
from rayito._agent._detect_scan import ProjectIndex, WarningSink, compiled_regex, render
from rayito._agent._domain import validate_model_id
from rayito._agent._project_domain import MODEL_ID_REQUIRED_PRESETS, AgentProjectException
from rayito.exceptions import InvalidArgumentException

CONFIG_RULE: Final = "config"
#: Evidencia de un valor que puso el usuario con una bandera.
OVERRIDE_PROVIDER_RULE: Final = "override:model_provider"
OVERRIDE_BASE_URL_RULE: Final = "override:model_base_url"
#: Cada clave de `model_envs` y el patrón de `modelFromCode` que la reconoce.
MODEL_ENV_PATTERNS: Final[tuple[tuple[str, str], ...]] = (
    ("id_env", "modelEnvPattern"),
    ("base_url_env", "baseUrlEnvPattern"),
    ("api_key_env", "apiKeyEnvPattern"),
)
#: Con sólo el nombre de la clave no hay modelo elegido por el código: hace
#: falta el del id o el de la URL base.
MODEL_CHOICE_KEYS: Final[tuple[str, ...]] = ("id_env", "base_url_env")
_MODEL_ID_PATH: Final = "model.id"
_MODEL_PROVIDER_PATH: Final = "model.provider"


@dataclass(frozen=True)
class ModelDetection:
    """Los proveedores (del más al menos probable), los literales de modelo
    y los nombres de variable con los que el código elige su modelo."""

    providers: tuple[DetectionProvider, ...]
    models: tuple[str, ...]
    model_envs: Mapping[str, str] = field(default_factory=dict)


def _env_files(index: ProjectIndex, rules: Mapping[str, Any], language: str | None) -> list[str]:
    named = [*rules["envScanFiles"], *rules["envExampleFiles"]]
    sources = index.sources(language) if language is not None else []
    return [*sources, *(name for name in named if index.has_root_file(name))]


def _env_mentions(
    index: ProjectIndex, rules: Mapping[str, Any], language: str | None
) -> dict[str, str]:
    """Nombre de variable -> el primer fichero que lo menciona."""
    names = sorted({env for provider in rules["providers"] for env in provider["envs"]})
    if not names:
        return {}
    pattern = compiled_regex(r"\b(" + "|".join(names) + r")\b")
    found: dict[str, str] = {}
    for path in _env_files(index, rules, language):
        text = index.read(path)
        if text is None:
            continue
        for match in pattern.finditer(text):
            found.setdefault(match.group(1), path)
    return found


def _dep_evidence(
    provider: Mapping[str, Any], key: str, language: str | None, deps: Mapping[str, str]
) -> list[DetectionEvidence]:
    names = provider.get(key, {}).get(language, []) if language is not None else []
    return [DetectionEvidence(deps[name], f"dep:{name}") for name in names if name in deps]


def _config_provider(
    index: ProjectIndex, rules: Mapping[str, Any], framework: str
) -> DetectionProvider | None:
    presets = {provider["preset"] for provider in rules["providers"]}
    for spec in rules["configProviders"]:
        if spec["framework"] != framework:
            continue
        for name in spec["files"]:
            text = index.read(name) if index.has_root_file(name) else None
            match = None if text is None else compiled_regex(spec["pattern"]).search(text)
            if match is None:
                continue
            preset = spec["aliases"].get(match.group(1).lower(), match.group(1).lower())
            if preset in presets:
                return DetectionProvider(preset, (DetectionEvidence(name, CONFIG_RULE),))
    return None


def _model_files(
    index: ProjectIndex, rules: Mapping[str, Any], language: str | None, framework: str
) -> list[str]:
    """Donde se buscan literales de modelo: el código del lenguaje y, para
    Hermes u OpenCode (que se configuran, no se programan), su fichero de
    configuración."""
    configs = [
        name
        for spec in rules["configProviders"]
        if spec["framework"] == framework
        for name in spec["files"]
        if index.has_root_file(name)
    ]
    return [*(index.sources(language) if language is not None else []), *configs]


def _models(
    index: ProjectIndex,
    rules: Mapping[str, Any],
    language: str | None,
    framework: str,
    presets: Sequence[str],
) -> tuple[str, ...]:
    patterns = [
        pattern
        for provider in rules["providers"]
        if provider["preset"] in presets
        for pattern in provider.get("models", [])
    ]
    found: set[str] = set()
    for path in _model_files(index, rules, language, framework):
        text = index.read(path)
        for pattern in patterns:
            if text is not None:
                found.update(match.group(1) for match in compiled_regex(pattern).finditer(text))
    return tuple(sorted(found)[: rules["maxModelsListed"]])


def detect_providers(
    index: ProjectIndex,
    rules: Mapping[str, Any],
    language: str | None,
    framework: str,
    deps: Mapping[str, str],
    sink: WarningSink,
    overrides: DetectOverrides | None = None,
) -> ModelDetection:
    """Los proveedores detectados, del más al menos probable, los literales
    de modelo que el código nombra y, sin literal, los nombres de variable
    del modelo. `--model-provider` y `--model-id` ganan a la detección."""
    overrides = overrides or DetectOverrides()
    detected = _detected_providers(index, rules, language, framework, deps)
    models = _models(index, rules, language, framework, [item.preset for item in detected])
    if overrides.model_id is not None:
        models = (_override_model_id(overrides.model_id),)
    if overrides.model_provider is not None:
        providers: tuple[DetectionProvider, ...] = (
            _override_provider(overrides.model_provider, rules),
        )
    else:
        providers = _with_base_url(detected, index, rules, language, overrides, models, sink)
        _warn(providers, sink)
    envs = _model_envs(index, rules, language, providers, models)
    if envs:
        sink.add("model_id_from_code", envs=", ".join(envs.values()))
    return ModelDetection(providers, models, envs)


def _detected_providers(
    index: ProjectIndex,
    rules: Mapping[str, Any],
    language: str | None,
    framework: str,
    deps: Mapping[str, str],
) -> tuple[DetectionProvider, ...]:
    mentions = _env_mentions(index, rules, language)
    strong: list[DetectionProvider] = []
    weak: list[DetectionProvider] = []
    configured = _config_provider(index, rules, framework)
    if configured is not None:
        strong.append(configured)
    for provider in rules["providers"]:
        evidence = _dep_evidence(provider, "deps", language, deps) + [
            DetectionEvidence(mentions[env], f"env:{env}")
            for env in provider["envs"]
            if env in mentions
        ]
        if evidence and provider["preset"] not in {item.preset for item in strong}:
            strong.append(DetectionProvider(provider["preset"], tuple(evidence)))
        weak_evidence = _dep_evidence(provider, "weakDeps", language, deps)
        if weak_evidence:
            weak.append(DetectionProvider(provider["preset"], tuple(weak_evidence)))
    return tuple(strong or weak)


def _override_model_id(model_id: str) -> str:
    try:
        return validate_model_id(model_id, "--model-id")
    except InvalidArgumentException as exc:
        raise AgentProjectException(
            f"{_MODEL_ID_PATH}: {exc}", path=_MODEL_ID_PATH, code="invalid_value"
        ) from None


def _override_provider(preset: str, rules: Mapping[str, Any]) -> DetectionProvider:
    presets = [provider["preset"] for provider in rules["providers"]]
    if preset not in presets:
        text = render(
            rules["messages"]["invalid_model_provider"],
            {"value": preset, "candidates": ", ".join(presets)},
        )
        raise AgentProjectException(
            f"{_MODEL_PROVIDER_PATH}: {text}", path=_MODEL_PROVIDER_PATH, code="invalid_value"
        )
    return DetectionProvider(preset, (DetectionEvidence(None, OVERRIDE_PROVIDER_RULE),))


def _custom_base_url(text: str, rule: Mapping[str, Any], official: Mapping[str, Any]) -> bool:
    """Si algún constructor de cliente de `text` pasa una URL base propia:
    una variable, una expresión o un literal que no es la URL oficial
    (`officialBaseUrl`). El grupo 1 de `rule` es el valor tal como se
    escribe, vacío si no lo hay (`{ baseURL }` en TypeScript)."""
    official_url = compiled_regex(official["pattern"])
    return any(
        not official_url.search(match.group(1) or "")
        for match in compiled_regex(rule["pattern"]).finditer(text)
    )


def _base_url_evidence(
    index: ProjectIndex,
    rules: Mapping[str, Any],
    language: str | None,
    overrides: DetectOverrides,
) -> DetectionEvidence | None:
    """De dónde sale una URL base propia: el primer fichero cuyo constructor
    de cliente la pasa (`clientBaseUrl`) o, si ninguno, `--model-base-url`."""
    spec = rules["modelFromCode"]
    rule = spec["clientBaseUrl"].get(language) if language is not None else None
    if rule is not None:
        for path in index.sources(str(language)):
            text = index.read(path)
            if text is not None and _custom_base_url(text, rule, spec["officialBaseUrl"]):
                return DetectionEvidence(path, spec["baseUrlRule"])
    if overrides.model_base_url is not None:
        return DetectionEvidence(None, OVERRIDE_BASE_URL_RULE)
    return None


def _with_base_url(
    providers: tuple[DetectionProvider, ...],
    index: ProjectIndex,
    rules: Mapping[str, Any],
    language: str | None,
    overrides: DetectOverrides,
    models: tuple[str, ...],
    sink: WarningSink,
) -> tuple[DetectionProvider, ...]:
    """`fromPreset` pasa a ser `toPreset` (en su sitio, sin añadir otro
    proveedor) si el cliente apunta a una URL base propia. Si el código fija
    el id del modelo, el proyecto ya se desplegaba con `fromPreset` antes de
    ADR-033: sólo cambia con `--model-base-url`, y sin ella lo avisa
    `custom_base_url`."""
    spec = rules["modelFromCode"]
    if not any(provider.preset == spec["fromPreset"] for provider in providers):
        return providers
    evidence = _base_url_evidence(index, rules, language, overrides)
    if evidence is None:
        return providers
    if models and overrides.model_base_url is None:
        sink.add("custom_base_url", path=str(evidence.path), preset=spec["fromPreset"])
        return providers
    return tuple(
        DetectionProvider(spec["toPreset"], (*provider.evidence, evidence))
        if provider.preset == spec["fromPreset"]
        else provider
        for provider in providers
    )


def _matches(pattern: str, text: str | None) -> list[str]:
    if text is None:
        return []
    return [match.group(1) for match in compiled_regex(pattern).finditer(text)]


def _settings_names(text: str | None, settings: Mapping[str, Any] | None) -> list[str]:
    """Los campos de una clase de pydantic-settings como variables:
    `env_prefix` más el nombre del campo, en mayúsculas."""
    if (
        text is None
        or settings is None
        or not compiled_regex(settings["marker"]["pattern"]).search(text)
    ):
        return []
    prefixes = _matches(settings["prefix"]["pattern"], text)
    prefix = prefixes[0] if prefixes else ""
    return [f"{prefix}{name}".upper() for name in _matches(settings["field"]["pattern"], text)]


def _langgraph_env_keys(index: ProjectIndex, name: str) -> list[str]:
    """Las claves del mapa `env` de `langgraph.json`; nunca sus valores."""
    document = index.read_json(name) if index.has_root_file(name) else None
    env = document.get("env") if isinstance(document, Mapping) else None
    return [str(key) for key in env] if isinstance(env, Mapping) else []


def _env_names(index: ProjectIndex, rules: Mapping[str, Any], language: str | None) -> list[str]:
    """Los nombres de variable que el proyecto declara o lee, ordenados."""
    spec = rules["modelFromCode"]
    names: set[str] = set()
    for name in rules["envExampleFiles"]:
        if index.has_root_file(name):
            names.update(_matches(spec["envExampleName"]["pattern"], index.read(name)))
    if language is not None:
        reads: Iterable[Mapping[str, str]] = spec["envReads"].get(language, [])
        settings = spec["settings"].get(language)
        for path in index.sources(language):
            text = index.read(path)
            for read in reads:
                names.update(_matches(read["pattern"], text))
            names.update(_settings_names(text, settings))
    names.update(_langgraph_env_keys(index, spec["langgraphJson"]))
    return sorted(names)


def _model_envs(
    index: ProjectIndex,
    rules: Mapping[str, Any],
    language: str | None,
    providers: tuple[DetectionProvider, ...],
    models: tuple[str, ...],
) -> dict[str, str]:
    """`model_envs` sólo cuando no hay literal, la pasarela no exige el id y
    el código nombra la variable del id o la de la URL base."""
    if models or not providers or providers[0].preset in MODEL_ID_REQUIRED_PRESETS:
        return {}
    spec = rules["modelFromCode"]
    names = _env_names(index, rules, language)
    found: dict[str, str] = {}
    for key, pattern_key in MODEL_ENV_PATTERNS:
        pattern = compiled_regex(spec[pattern_key]["pattern"])
        chosen = next((name for name in names if pattern.search(name)), None)
        if chosen is not None:
            found[key] = chosen
    return found if any(key in found for key in MODEL_CHOICE_KEYS) else {}


def _warn(providers: tuple[DetectionProvider, ...], sink: WarningSink) -> None:
    if not providers:
        sink.add("no_provider")
    elif len(providers) > 1:
        sink.add(
            "several_providers",
            providers=", ".join(provider.preset for provider in providers),
            used=providers[0].preset,
        )


__all__ = ["ModelDetection", "detect_providers"]
