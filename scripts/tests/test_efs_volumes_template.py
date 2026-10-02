"""`infra/efs-volumes.yaml` (M15, ADR-018), read as data. cfn-lint checks
the shape; these tests pin down the behaviour two earlier review rounds got
wrong and that cfn-lint cannot see on its own:

- there is exactly one file system, always `DeletionPolicy: Retain` /
  `UpdateReplacePolicy: Retain` as literals (never an `!If`, which needs
  the `AWS::LanguageExtensions` transform and `CAPABILITY_AUTO_EXPAND`,
  forbidden by `_stacks/_model.py`), with no `Condition` and no property
  that depends on a parameter: `OptionalStacks.deploy()` re-sends every
  default on `UpdateStack`, so a redeploy without parameters must never
  change which file-system resource exists (an earlier `RetainData` toggle
  chose between two resources and a plain redeploy could destroy the data).
- the connector's security group can actually reach the mount targets on
  NFS (2049): without this egress rule, EFS-3 would fail because of this
  template, not the platform.
- the identity-side Allow (`EfsVolumeClientPolicy`) never grants
  `elasticfilesystem:ClientRootAccess` and needs no `CAPABILITY_NAMED_IAM`.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
TEMPLATE = REPO_ROOT / "infra" / "efs-volumes.yaml"
NFS_PORT = 2049


class TemplateLoader(yaml.SafeLoader):
    """`SafeLoader` con las etiquetas cortas de CloudFormation resueltas a
    su forma larga (`!Ref X` queda como `{"Ref": "X"}`)."""


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


def test_the_template_declares_no_transform() -> None:
    """No `AWS::LanguageExtensions` (or anything else): that needs
    `CAPABILITY_AUTO_EXPAND`, which `_stacks/_model.py` forbids outright."""
    assert "Transform" not in template()


def file_systems() -> dict[str, Any]:
    return {
        name: resource
        for name, resource in resources().items()
        if resource["Type"] == "AWS::EFS::FileSystem"
    }


def referenced_names(node: Any) -> set[str]:
    """Every logical name a `Ref`/`Fn::GetAtt`/`Fn::If`/`Fn::Sub` under
    `node` mentions (a coarse superset: enough to prove "no parameter")."""
    if isinstance(node, dict):
        names: set[str] = set()
        for key, value in node.items():
            if key == "Ref" and isinstance(value, str):
                names.add(value)
            elif key == "Fn::Sub":
                names.update(re.findall(r"\$\{([\w.:]+)\}", str(value)))
            names |= referenced_names(value)
        return names
    if isinstance(node, list):
        return set().union(*(referenced_names(item) for item in node)) if node else set()
    return set()


def test_there_is_exactly_one_file_system_always_retained() -> None:
    assert list(file_systems()) == ["FileSystem"]
    file_system = file_systems()["FileSystem"]
    assert file_system["DeletionPolicy"] == "Retain"
    assert file_system["UpdateReplacePolicy"] == "Retain"
    assert "Condition" not in file_system


def test_a_redeploy_without_parameters_never_changes_the_file_system() -> None:
    """No parameter (default or not) can select, condition or reshape the
    file system, so `deploy()` re-sending every default on `UpdateStack`
    cannot replace it, delete it or swap it for another one."""
    file_system = file_systems()["FileSystem"]
    parameters = set(template()["Parameters"])
    assert not referenced_names(file_system) & parameters
    assert "RetainData" not in parameters


def test_mount_targets_and_outputs_reference_the_one_file_system() -> None:
    for mount_target in ("MountTarget1", "MountTarget2", "MountTarget3"):
        assert resources()[mount_target]["Properties"]["FileSystemId"] == {
            "Ref": "FileSystem"
        }
    assert template()["Outputs"]["FileSystemId"]["Value"] == {"Ref": "FileSystem"}


def test_connector_security_group_can_reach_the_mount_targets_on_nfs() -> None:
    """EFS-3 is a stop criterion; without this rule the connector's ENIs
    could never open an NFS connection regardless of what the platform
    allows (the implicit allow-all egress rule is gone as soon as
    `ConnectorSecurityGroup` declares its placeholder rule)."""
    egress = resources()["ConnectorEgressToMountTargets"]
    assert egress["Type"] == "AWS::EC2::SecurityGroupEgress"
    properties = egress["Properties"]
    assert properties["GroupId"] == {"Ref": "ConnectorSecurityGroup"}
    assert properties["DestinationSecurityGroupId"] == {
        "Ref": "MountTargetSecurityGroup"
    }
    assert properties["IpProtocol"] == "tcp"
    assert properties["FromPort"] == NFS_PORT
    assert properties["ToPort"] == NFS_PORT


def test_connector_egress_is_its_own_resource_not_inline() -> None:
    """A rule inline on either security group referencing the other's
    logical id is a circular reference CloudFormation rejects; it must stay
    a separate `AWS::EC2::SecurityGroupEgress`."""
    connector_sg = resources()["ConnectorSecurityGroup"]["Properties"]
    for rule in connector_sg.get("SecurityGroupEgress", []):
        assert "DestinationSecurityGroupId" not in rule


def test_efs_volume_client_policy_never_grants_root_access_and_needs_no_name() -> None:
    policy = resources()["EfsVolumeClientPolicy"]
    assert policy["Type"] == "AWS::IAM::ManagedPolicy"
    assert "ManagedPolicyName" not in policy["Properties"]
    statement = policy["Properties"]["PolicyDocument"]["Statement"][0]
    actions = {
        action
        for action in statement["Action"]
        if action
        != {
            "Fn::If": [
                "GrantClientWrite",
                "elasticfilesystem:ClientWrite",
                {"Ref": "AWS::NoValue"},
            ]
        }
    }
    assert actions == {"elasticfilesystem:ClientMount"}
    assert "elasticfilesystem:ClientRootAccess" not in str(policy)


def test_caller_policy_statement_only_claims_pass_network_connector() -> None:
    """Regression for the earlier review round: the description used to
    claim ClientMount/ClientWrite coverage for a value that was only
    `lambda:PassNetworkConnector`. It may still *name* those actions to say
    it does not cover them, but the `Value` itself must stay exactly the
    pass-connector statement, and the real grant lives in `CallerPolicyArn`."""
    statement = template()["Outputs"]["CallerPolicyStatement"]
    value = statement["Value"]["Fn::Sub"]
    assert "PassNetworkConnector" in statement["Description"]
    assert "ClientMount" not in value
    assert "ClientWrite" not in value
    assert "lambda:PassNetworkConnector" in value
    assert "CallerPolicyArn" in template()["Outputs"]
