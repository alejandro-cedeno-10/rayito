"""`infra/metadata-index.yaml` (M14), leída como datos: el único recurso de
datos es la tabla DynamoDB (on-demand, clave `pk`, TTL en `expires_at`, sin
streams); las dos políticas sólo conceden `dynamodb:PutItem` (escritor) y
`dynamodb:BatchGetItem` (lector) sobre el ARN de la tabla, nunca `*`; ni
Lambdas, ni streams, ni EventBridge. Además, las operaciones de DynamoDB que
nombran `clients/python/src/rayito/_index.py` y
`clients/typescript/src/index/**` son exactamente las de
`AWS_API_NOTES.md` §20. `cfn-lint` valida la forma, no la política."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
TEMPLATE = REPO_ROOT / "infra" / "metadata-index.yaml"
PYTHON_INDEX = REPO_ROOT / "clients" / "python" / "src" / "rayito" / "_index.py"
TS_INDEX_DIR = REPO_ROOT / "clients" / "typescript" / "src" / "index"
AWS_NOTES = REPO_ROOT / "AWS_API_NOTES.md"
TABLE_ARN = {"Fn::GetAtt": "MetadataIndexTable.Arn"}
DOCUMENTED_OPERATIONS = frozenset({"PutItem", "BatchGetItem"})


class TemplateLoader(yaml.SafeLoader):
    """`SafeLoader` con las etiquetas cortas de CloudFormation resueltas a
    su forma larga (`!GetAtt A.B` queda como cadena `A.B`)."""


def construct_short_tag(loader: yaml.SafeLoader, suffix: str, node: yaml.Node) -> Any:
    name = suffix if suffix in {"Ref", "Condition"} else f"Fn::{suffix}"
    if isinstance(node, yaml.ScalarNode):
        return {name: loader.construct_scalar(node)}
    if isinstance(node, yaml.SequenceNode):
        return {name: loader.construct_sequence(node, deep=True)}
    return {name: loader.construct_mapping(node, deep=True)}


TemplateLoader.add_multi_constructor("!", construct_short_tag)


def template() -> dict[str, Any]:
    document = yaml.load(TEMPLATE.read_text(encoding="utf-8"), Loader=TemplateLoader)
    assert isinstance(document, dict)
    return document


def resources() -> dict[str, Any]:
    return dict(template()["Resources"])


def statements(policy: str) -> list[dict[str, Any]]:
    return list(resources()[policy]["Properties"]["PolicyDocument"]["Statement"])


def as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else [value]


def test_the_only_resources_are_the_table_and_two_policies() -> None:
    kinds = {name: body["Type"] for name, body in resources().items()}
    assert kinds == {
        "MetadataIndexTable": "AWS::DynamoDB::Table",
        "RayitoIndexWriter": "AWS::IAM::ManagedPolicy",
        "RayitoIndexReader": "AWS::IAM::ManagedPolicy",
    }


def test_the_table_is_on_demand_keyed_by_pk_with_ttl_on_expires_at() -> None:
    table = resources()["MetadataIndexTable"]["Properties"]
    assert table["BillingMode"] == "PAY_PER_REQUEST"
    assert table["KeySchema"] == [{"AttributeName": "pk", "KeyType": "HASH"}]
    assert table["AttributeDefinitions"] == [
        {"AttributeName": "pk", "AttributeType": "S"}
    ]
    assert table["TimeToLiveSpecification"] == {
        "AttributeName": "expires_at",
        "Enabled": True,
    }
    assert "StreamSpecification" not in table
    assert "ProvisionedThroughput" not in table
    assert "GlobalSecondaryIndexes" not in table


def test_defaults_cost_nothing_at_rest() -> None:
    parameters = template()["Parameters"]
    assert parameters["TableName"]["Default"] == "rayito-sandboxes"
    assert parameters["PointInTimeRecovery"]["Default"] == "false"
    assert parameters["DeletionProtection"]["Default"] == "false"


def test_writer_and_reader_only_get_their_action_on_the_table() -> None:
    expected = {
        "RayitoIndexWriter": ["dynamodb:PutItem"],
        "RayitoIndexReader": ["dynamodb:BatchGetItem"],
    }
    for policy, actions in expected.items():
        for statement in statements(policy):
            assert statement["Effect"] == "Allow"
            assert as_list(statement["Action"]) == actions
            assert statement["Resource"] == TABLE_ARN
            assert "*" not in str(statement["Resource"])


def test_no_lambda_stream_or_eventbridge_anywhere() -> None:
    text = TEMPLATE.read_text(encoding="utf-8")
    for forbidden in (
        "AWS::Lambda",
        "AWS::Events",
        "StreamSpecification",
        "AWS::Pipes",
    ):
        assert forbidden not in text


def sdk_operation_names() -> set[str]:
    python = PYTHON_INDEX.read_text(encoding="utf-8")
    names = {
        "".join(part.capitalize() for part in method.split("_"))
        for method in re.findall(
            r"\.(put_item|batch_get_item|delete_item|get_item|query|scan)\(", python
        )
    }
    for path in TS_INDEX_DIR.rglob("*.ts"):
        names.update(
            re.findall(r"new sdk\.(\w+)Command\(", path.read_text(encoding="utf-8"))
        )
    return names


def test_the_sdks_only_name_operations_documented_in_section_20() -> None:
    notes = AWS_NOTES.read_text(encoding="utf-8")
    match = re.search(
        r"^## 20\. DynamoDB.*?(?=^## \d+\.|\Z)", notes, re.DOTALL | re.MULTILINE
    )
    assert match, "AWS_API_NOTES.md: falta el apartado ## 20. DynamoDB"
    section = match.group(0)
    used = sdk_operation_names()
    assert used == DOCUMENTED_OPERATIONS, used
    for operation in used:
        assert f"`{operation}`" in section, operation
    assert "DeleteItem" not in used


def read(relative: str) -> str:
    return (REPO_ROOT / relative).read_text(encoding="utf-8")


def test_security_threat_model_has_t19() -> None:
    rows = [
        line for line in read("SECURITY.md").splitlines() if line.startswith("| T19 |")
    ]
    assert len(rows) == 1, (
        "SECURITY.md: falta la fila T19 (copia de metadatos en reposo)"
    )
    row = rows[0]
    for phrase in (
        "nunca",
        "access token",
        "fantasma",
        "RayitoIndexWriter",
        "RayitoIndexReader",
    ):
        assert phrase in row, f"SECURITY.md T19 debe mencionar «{phrase}»"
    assert "T19" in read("docs/site/docs/security.md")


def test_parity_row_40_is_divergent_with_the_index() -> None:
    rows = [
        line
        for line in read("docs/site/docs/e2b-parity.md").splitlines()
        if line.startswith("| 40 |")
    ]
    assert len(rows) == 1
    status = rows[0].split(" | ")[2]
    assert status.startswith("divergente")
    assert "index=DynamoDbIndex" in rows[0]
    assert "UnimplementedError" in rows[0]


def test_docs_explain_the_index_and_its_cost() -> None:
    observability = read("docs/site/docs/observability.md")
    assert "## Listado por metadatos con índice (opcional)" in observability
    assert "--index-table" in read("docs/site/docs/cli.md")
    assert "::: rayito.DynamoDbIndex" in read("docs/site/docs/referencia/python/opcionales.md")
    assert "Coste y activación" in read(
        "docs/site/docs/funciones-opcionales/indice-de-metadatos.md"
    )
    assert "metadata-index.yaml" in read("infra/README.md")
    page = read("docs/site/docs/optional-features.md")
    row = next(
        line
        for line in page.splitlines()
        if "(#metadata-index)" in line and "|" in line
    )
    assert "clients/python/src/rayito/_index.py" in row
    assert "clients/typescript/src/index/dynamodb.ts" in row
