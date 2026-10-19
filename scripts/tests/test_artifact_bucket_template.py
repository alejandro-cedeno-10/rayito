"""`infra/artifact-bucket.yaml` (ADR-030, `rayito setup`), leída como datos:
el bucket de artefactos que `rayito setup` crea cuando no hay ninguno que
reutilizar. Fija lo que `cfn-lint` no comprueba: cifrado SSE-S3, acceso
público bloqueado, ACLs desactivadas, sólo TLS, la caducidad de
`rayito/images/` y que borrar la pila no borra el bucket. Los nombres que
comparte con los SDKs (parámetro, salida, días, prefijo y nombre por defecto)
se comparan con `testdata/account-setup/contract.json`."""

from __future__ import annotations

import json
import re
from typing import Any

from cfn_yaml import REPO_ROOT, as_list, load_template

CONTRACT = json.loads(
    (REPO_ROOT / "testdata" / "account-setup" / "contract.json").read_text(encoding="utf-8")
)
CONSTANTS: dict[str, Any] = CONTRACT["constants"]
LIMITS = json.loads((REPO_ROOT / "limits.json").read_text(encoding="utf-8"))
PLACEHOLDER_ACCOUNT = "123456789012"
BUCKET = "ArtifactBucket"
POLICY = "ArtifactBucketPolicy"
RETAIN = "Retain"


def template() -> dict[str, Any]:
    return load_template("artifact-bucket")


def bucket_properties() -> dict[str, Any]:
    return dict(template()["Resources"][BUCKET]["Properties"])


def test_only_the_bucket_and_its_policy_are_created() -> None:
    resources = template()["Resources"]
    assert {name: item["Type"] for name, item in resources.items()} == {
        BUCKET: "AWS::S3::Bucket",
        POLICY: "AWS::S3::BucketPolicy",
    }


def test_deleting_or_replacing_the_stack_keeps_the_bucket() -> None:
    bucket = template()["Resources"][BUCKET]
    assert bucket["DeletionPolicy"] == RETAIN
    assert bucket["UpdateReplacePolicy"] == RETAIN


def test_the_bucket_is_encrypted_private_and_without_acls() -> None:
    properties = bucket_properties()
    rules = properties["BucketEncryption"]["ServerSideEncryptionConfiguration"]
    assert [rule["ServerSideEncryptionByDefault"]["SSEAlgorithm"] for rule in rules] == ["AES256"]
    assert properties["PublicAccessBlockConfiguration"] == {
        "BlockPublicAcls": True,
        "BlockPublicPolicy": True,
        "IgnorePublicAcls": True,
        "RestrictPublicBuckets": True,
    }
    assert properties["OwnershipControls"] == {
        "Rules": [{"ObjectOwnership": "BucketOwnerEnforced"}]
    }
    assert "VersioningConfiguration" not in properties
    assert "AccessControl" not in properties


def test_only_the_image_artifacts_expire_after_the_contract_days() -> None:
    rules = bucket_properties()["LifecycleConfiguration"]["Rules"]
    assert len(rules) == 1
    rule = rules[0]
    assert rule["Status"] == "Enabled"
    assert rule["Prefix"] == CONSTANTS["imageArtifactPrefix"]
    assert rule["ExpirationInDays"] == {"Ref": "ArtifactExpirationDays"}
    parameter = template()["Parameters"]["ArtifactExpirationDays"]
    assert parameter["Default"] == CONSTANTS["artifactExpirationDays"]
    assert parameter["MinValue"] == 1


def test_the_policy_only_denies_plain_http() -> None:
    policy = template()["Resources"][POLICY]["Properties"]
    assert policy["Bucket"] == {"Ref": BUCKET}
    statements = policy["PolicyDocument"]["Statement"]
    assert len(statements) == 1
    statement = statements[0]
    assert statement["Effect"] == "Deny"
    assert statement["Principal"] == "*"
    assert as_list(statement["Action"]) == ["s3:*"]
    assert statement["Condition"] == {"Bool": {"aws:SecureTransport": "false"}}
    assert as_list(statement["Resource"]) == [
        {"Fn::GetAtt": f"{BUCKET}.Arn"},
        {"Fn::Sub": "${" + BUCKET + ".Arn}/*"},
    ]


def test_the_parameter_and_output_names_match_the_contract() -> None:
    document = template()
    assert set(document["Parameters"]) == {
        CONSTANTS["bucketNameParameter"],
        "ArtifactExpirationDays",
    }
    assert document["Resources"][BUCKET]["Properties"]["BucketName"] == {
        "Ref": CONSTANTS["bucketNameParameter"]
    }
    assert document["Outputs"][CONSTANTS["bucketNameOutputKey"]]["Value"] == {"Ref": BUCKET}


def test_the_default_bucket_name_is_valid_in_every_supported_region() -> None:
    parameter = template()["Parameters"][CONSTANTS["bucketNameParameter"]]
    pattern = re.compile(parameter["AllowedPattern"])
    assert parameter["MinLength"] == LIMITS["s3BucketNameMin"]
    assert parameter["MaxLength"] == LIMITS["s3BucketNameMax"]
    for region in LIMITS["supportedRegions"]:
        name = CONSTANTS["defaultArtifactBucketTemplate"].format(
            account=PLACEHOLDER_ACCOUNT, region=region
        )
        assert pattern.fullmatch(name), name
        assert parameter["MinLength"] <= len(name) <= parameter["MaxLength"], name
