"""`infra/custom-domain.yaml` embebe `infra/functions/custom_domain_router.js`
literalmente bajo `FunctionCode` (m15-custom-domain, ADR-024): este test es
la guarda contra que diverjan, ya que `AWS::CloudFront::Function` sólo
acepta el código como una cadena dentro de la plantilla (CloudFormation no
tiene un `Fn::Include` de fichero), así que el `.js` es la fuente de verdad
que `infra/functions/tests/custom_domain_router.test.mjs` prueba con
`node:test`, y este test comprueba que la plantilla trae exactamente ese
mismo texto, no una copia que se haya quedado atrás."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
TEMPLATE = REPO_ROOT / "infra" / "custom-domain.yaml"
ROUTER_JS = REPO_ROOT / "infra" / "functions" / "custom_domain_router.js"


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


def test_function_code_matches_the_source_file_byte_for_byte() -> None:
    document = _template()
    embedded = document["Resources"]["RouterFunction"]["Properties"]["FunctionCode"]
    source = ROUTER_JS.read_text(encoding="utf-8")
    assert embedded.rstrip("\n") == source.rstrip("\n"), (
        "infra/custom-domain.yaml FunctionCode quedó desincronizado de "
        "infra/functions/custom_domain_router.js: regenera el bloque "
        "FunctionCode con el contenido exacto de ese fichero"
    )


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
    # Managed "CachingDisabled" / "AllViewer" policy ids (AWS_API_NOTES.md
    # section 29): routing is per sandbox, so nothing here may be cached.
    assert behavior["CachePolicyId"] == "4135ea2d-6df8-44a3-9df3-4b5a84be39ad"
    assert behavior["OriginRequestPolicyId"] == "216adef6-5c7f-47e4-b989-5492eafa07d3"
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
