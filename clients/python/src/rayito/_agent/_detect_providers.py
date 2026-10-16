"""Proveedores de modelo de un proyecto (ADR-029): por sus dependencias, por
los nombres de variable de entorno que el código, el README o un
`.env.example` mencionan y por la configuración de Hermes u OpenCode. Cada
proveedor es un preset de pasarela existente (`bedrock_gateway`,
`openai_gateway`...). Sólo se leen nombres: los valores de un `.env` nunca se
leen ni se guardan, y los literales de modelo son identificadores públicos.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Final

from rayito._agent._detect_report import DetectionEvidence, DetectionProvider
from rayito._agent._detect_scan import ProjectIndex, WarningSink, compiled_regex

CONFIG_RULE: Final = "config"


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


def _models(
    index: ProjectIndex, rules: Mapping[str, Any], language: str | None, presets: Sequence[str]
) -> tuple[str, ...]:
    patterns = [
        pattern
        for provider in rules["providers"]
        if provider["preset"] in presets
        for pattern in provider.get("models", [])
    ]
    found: set[str] = set()
    for path in index.sources(language) if language is not None else []:
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
) -> tuple[tuple[DetectionProvider, ...], tuple[str, ...]]:
    """Los proveedores detectados, del más al menos probable, y los
    literales de modelo que el código nombra."""
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
    providers = tuple(strong or weak)
    _warn(providers, sink)
    presets = [provider.preset for provider in providers]
    return providers, _models(index, rules, language, presets)


def _warn(providers: tuple[DetectionProvider, ...], sink: WarningSink) -> None:
    if not providers:
        sink.add("no_provider")
    elif len(providers) > 1:
        sink.add(
            "several_providers",
            providers=", ".join(provider.preset for provider in providers),
            used=providers[0].preset,
        )


__all__ = ["detect_providers"]
