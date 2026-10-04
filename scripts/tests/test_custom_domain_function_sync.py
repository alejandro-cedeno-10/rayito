"""`infra/custom-domain.yaml` embebe bajo `FunctionCode` una versión
derivada de `infra/functions/custom_domain_router.js` (m15-custom-domain,
ADR-024): este test es la guarda contra que diverjan, ya que
`AWS::CloudFront::Function` sólo acepta el código como una cadena dentro de
la plantilla (CloudFormation no tiene un `Fn::Include` de fichero), así que
el `.js` es la fuente de verdad que `infra/functions/tests/
custom_domain_router.test.mjs` prueba con `node:test`.

"Derivada", no idéntica: `export ` se quita de cada declaración de nivel
superior (`_strip_exports`, abajo) antes de comparar. `export const`/
`export function` sólo existen en el fichero fuente para que los tests de
Node importen sus funciones puras directamente; el runtime
`cloudfront-js-2.0` nunca ha documentado soporte para esa sintaxis de
módulos ES en el código de la propia función (a diferencia de
`import cf from "cloudfront"`, que sí es la forma documentada por AWS de
acceder a sus builtins, y por eso se conserva tal cual)."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
TEMPLATE = REPO_ROOT / "infra" / "custom-domain.yaml"
ROUTER_JS = REPO_ROOT / "infra" / "functions" / "custom_domain_router.js"
LIMITS_JSON = REPO_ROOT / "limits.json"

#: Sólo estas tres formas de declaración de nivel superior usan `export` en
#: `custom_domain_router.js` (comprobado por inspección: ni `export default`
#: ni un `export {...}` de cola aparecen en ese fichero); quitarlas es lo
#: único que distingue el código fuente del `FunctionCode` desplegado.
_EXPORTED_DECLARATION_KEYWORDS = ("const", "function", "async function")


def _strip_exports(source: str) -> str:
    """El código que de verdad se despliega: sin `export ` en ninguna
    declaración de nivel superior (ver el docstring del módulo)."""
    pattern = re.compile(
        rf"^export (?=(?:{'|'.join(_EXPORTED_DECLARATION_KEYWORDS)})\b)", re.MULTILINE
    )
    return pattern.sub("", source)


class TemplateLoader(yaml.SafeLoader):
    """`SafeLoader` con las etiquetas cortas de CloudFormation resueltas a
    su forma larga, igual que `test_metadata_index_template.py`."""


def _construct_short_tag(loader: yaml.SafeLoader, suffix: str, node: yaml.Node) -> Any:
    name = suffix if suffix in {"Ref", "Condition"} else f"Fn::{suffix}"
    if isinstance(node, yaml.ScalarNode):
        return {name: loader.construct_scalar(node)}
    if isinstance(node, yaml.SequenceNode):
        return {name: loader.construct_sequence(node, deep=True)}
    return {name: loader.construct_mapping(node, deep=True)}


TemplateLoader.add_multi_constructor("!", _construct_short_tag)


def _template() -> dict[str, Any]:
    document = yaml.load(TEMPLATE.read_text(encoding="utf-8"), Loader=TemplateLoader)
    assert isinstance(document, dict)
    return document


def test_function_code_matches_the_stripped_source_file() -> None:
    document = _template()
    embedded = document["Resources"]["RouterFunction"]["Properties"]["FunctionCode"]
    expected = _strip_exports(ROUTER_JS.read_text(encoding="utf-8"))
    assert embedded.rstrip("\n") == expected.rstrip("\n"), (
        "infra/custom-domain.yaml FunctionCode quedó desincronizado de "
        "infra/functions/custom_domain_router.js (sin sus `export `): "
        "regenera el bloque FunctionCode con _strip_exports(ese fichero)"
    )


#: Sintaxis que `cloudfront-js-2.0` rechaza al compilar y Node acepta
#: (medido con `TestFunction`, Q121 de AWS_API_NOTES.md): `for...of` y los
#: parámetros por defecto. Los tests de Node nunca lo detectarían.
_UNSUPPORTED_RUNTIME_SYNTAX = {
    "for...of": re.compile(r"\bfor\s*\([^)]*\bof\b"),
    "parámetro por defecto": re.compile(r"\bfunction\b[^(]*\([^)]*="),
}


def _code_lines(source: str) -> list[str]:
    """Las líneas de código, sin los comentarios `//` que citan la regla."""
    comment_prefixes = ("//", "*", "/**")
    return [line for line in source.splitlines() if not line.lstrip().startswith(comment_prefixes)]


def test_function_code_avoids_syntax_the_runtime_rejects() -> None:
    document = _template()
    embedded = document["Resources"]["RouterFunction"]["Properties"]["FunctionCode"]
    for line in _code_lines(embedded):
        for name, pattern in _UNSUPPORTED_RUNTIME_SYNTAX.items():
            assert not pattern.search(line), (
                f"{name} no compila en cloudfront-js-2.0: {line.strip()}"
            )


def test_function_code_has_no_es_module_syntax() -> None:
    """El runtime `cloudfront-js-2.0` nunca ha documentado soporte para
    `export` en el código de la propia función; si `CreateFunction` lo
    rechazara, el despliegue entero de la pila fallaría (hallazgo del
    review de PR #74). `_strip_exports` es idempotente sobre un texto que ya
    no tiene ninguna declaración exportada; no se busca la subcadena
    "export " a pelo porque los comentarios del fichero la mencionan al
    explicar justo esta regla."""
    document = _template()
    embedded = document["Resources"]["RouterFunction"]["Properties"]["FunctionCode"]
    assert _strip_exports(embedded) == embedded


def test_reserved_ports_match_limits_json() -> None:
    """`RESERVED_PORTS` en `custom_domain_router.js` no puede importar
    `limits.json` (el runtime de CloudFront Functions sólo resuelve sus
    propios builtins) ni el módulo generado que sí usan `_limits.py`/
    `limits.ts`; este test es la única guarda contra que ese literal se
    quede atrás si `reservedPorts` cambia alguna vez."""
    import json

    limits = json.loads(LIMITS_JSON.read_text(encoding="utf-8"))
    source = ROUTER_JS.read_text(encoding="utf-8")
    match = re.search(r"export const RESERVED_PORTS = (\[[^\]]*\]);", source)
    assert match is not None, "no se encontró el literal RESERVED_PORTS en custom_domain_router.js"
    reserved_in_router = json.loads(match.group(1))
    assert reserved_in_router == limits["reservedPorts"]


def test_router_function_uses_the_kvs_runtime_and_is_associated() -> None:
    document = _template()
    config = document["Resources"]["RouterFunction"]["Properties"]["FunctionConfig"]
    assert config["Runtime"] == "cloudfront-js-2.0"
    associations = config["KeyValueStoreAssociations"]
    assert associations == [{"KeyValueStoreARN": {"Fn::GetAtt": "RouteKeyValueStore.Arn"}}]


def test_default_cache_behavior_is_never_cached_and_runs_the_function() -> None:
    document = _template()
    behavior = document["Resources"]["Distribution"]["Properties"]["DistributionConfig"][
        "DefaultCacheBehavior"
    ]
    # Managed "CachingDisabled" / "AllViewerExceptHostHeader" policy ids
    # (AWS_API_NOTES.md section 29): routing is per sandbox, so nothing here
    # may be cached, and Host must never reach the chosen origin (DOM-2) —
    # plain "AllViewer" would forward it and break the TLS/SNI check
    # against the MicroVMs endpoint's own certificate.
    assert behavior["CachePolicyId"] == "4135ea2d-6df8-44a3-9df3-4b5a84be39ad"
    assert behavior["OriginRequestPolicyId"] == "b689b0a8-53d0-40ab-baf2-68738e2966ac"
    assert behavior["FunctionAssociations"] == [
        {
            "EventType": "viewer-request",
            "FunctionARN": {"Fn::GetAtt": "RouterFunction.FunctionMetadata.FunctionARN"},
        }
    ]


def test_distribution_has_no_lambda_or_iam_resources() -> None:
    document = _template()
    types = {resource["Type"] for resource in document["Resources"].values()}
    assert types == {
        "AWS::CloudFront::KeyValueStore",
        "AWS::CloudFront::Function",
        "AWS::CloudFront::Distribution",
    }
