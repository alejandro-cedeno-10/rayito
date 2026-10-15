"""`infra/dashboard.yaml` (ADR-026, fase 2), leída como datos: la política no
la valida `cfn-lint` (ver `make infra-lint`), así que se fija aquí. Dos roles
separados, tabla de auditoría sólo de añadir, nada que el SCP de la cuenta de
pruebas deniegue (sin CloudFront ni bucket) y los números de la plantilla
iguales a los de `limits.json`."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
TEMPLATE = REPO_ROOT / "infra" / "dashboard.yaml"
LIMITS = json.loads((REPO_ROOT / "limits.json").read_text(encoding="utf-8"))
WRITE_ACTIONS = frozenset(
    {"lambda:TerminateMicrovm", "lambda:SuspendMicrovm", "lambda:ResumeMicrovm"}
)
FORBIDDEN_RESOURCE_TYPES = frozenset({"AWS::CloudFront::Distribution", "AWS::S3::Bucket"})


class TemplateLoader(yaml.SafeLoader):
    pass


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


def statements(role: str) -> list[dict[str, Any]]:
    """Las sentencias del rol, con las condicionales (`Fn::If`) aplanadas."""
    found: list[dict[str, Any]] = []
    for policy in resources()[role]["Properties"]["Policies"]:
        for item in policy["PolicyDocument"]["Statement"]:
            found.append(item["Fn::If"][1] if "Fn::If" in item else item)
    return found


def as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else [value]


def allowed_actions(role: str) -> set[str]:
    return {
        action
        for item in statements(role)
        if item["Effect"] == "Allow"
        for action in as_list(item["Action"])
    }


def test_it_creates_nothing_the_test_account_scp_denies() -> None:
    types = {resource["Type"] for resource in resources().values()}
    assert not types & FORBIDDEN_RESOURCE_TYPES
    function = resources()["DashboardFunction"]["Properties"]
    assert "S3Key" in function["Code"] and "ZipFile" not in function["Code"]


def test_the_function_url_is_the_only_unauthenticated_door_by_default_and_says_so() -> None:
    url = resources()["FunctionUrl"]["Properties"]
    assert url["AuthType"] == {"Fn::If": ["IamAuth", "AWS_IAM", "NONE"]}
    assert url["Cors"]["Fn::If"][0] == "IamAuth"
    assert url["Cors"]["Fn::If"][2] == {"Ref": "AWS::NoValue"}
    assert resources()["FunctionUrl"]["Type"] == "AWS::Lambda::Url"


def test_the_read_role_cannot_act_on_sandboxes() -> None:
    actions = allowed_actions("ReadRole")
    assert not actions & WRITE_ACTIONS
    assert "lambda:ListMicrovms" in actions
    assert not any(action.endswith("*") for action in actions)


def test_only_the_actions_role_can_terminate_suspend_or_resume() -> None:
    assert allowed_actions("ActionsRole") == WRITE_ACTIONS
    for item in statements("ActionsRole"):
        assert "*" != item["Resource"]


def test_the_actions_role_trusts_only_the_read_role_and_is_only_created_on_request() -> None:
    role = resources()["ActionsRole"]
    assert role["Condition"] == "ActionsEnabled"
    (statement,) = role["Properties"]["AssumeRolePolicyDocument"]["Statement"]
    assert statement["Principal"] == {"AWS": {"Fn::GetAtt": "ReadRole.Arn"}}


def test_the_function_may_assume_the_actions_role_and_nothing_else() -> None:
    policy = resources()["AssumeActionsRolePolicy"]
    assert policy["Condition"] == "ActionsEnabled"
    (statement,) = policy["Properties"]["PolicyDocument"]["Statement"]
    assert statement["Action"] == "sts:AssumeRole"
    assert statement["Resource"] == {"Fn::GetAtt": "ActionsRole.Arn"}


def test_the_application_can_append_to_the_audit_table_but_never_change_or_delete() -> None:
    audit_statements = [
        item
        for item in statements("ReadRole")
        if item.get("Resource") == {"Fn::GetAtt": "AuditTable.Arn"}
    ]
    allows = {a for s in audit_statements if s["Effect"] == "Allow" for a in as_list(s["Action"])}
    denies = {a for s in audit_statements if s["Effect"] == "Deny" for a in as_list(s["Action"])}
    assert allows == {"dynamodb:PutItem"}
    assert {"dynamodb:UpdateItem", "dynamodb:DeleteItem", "dynamodb:BatchWriteItem"} <= denies


def test_the_audit_and_cache_tables_are_on_demand_and_the_cache_expires_by_ttl() -> None:
    audit = resources()["AuditTable"]["Properties"]
    cache = resources()["CacheTable"]["Properties"]
    assert audit["BillingMode"] == cache["BillingMode"] == "PAY_PER_REQUEST"
    assert [key["AttributeName"] for key in audit["KeySchema"]] == ["pk", "sk"]
    assert cache["TimeToLiveSpecification"] == {"AttributeName": "expires_at", "Enabled": True}


def test_billed_cost_permissions_exist_only_when_cost_explorer_is_enabled() -> None:
    conditional = {
        item["Fn::If"][1]["Sid"]: item["Fn::If"][0]
        for policy in resources()["ReadRole"]["Properties"]["Policies"]
        for item in policy["PolicyDocument"]["Statement"]
        if "Fn::If" in item
    }
    assert conditional["ReadBilledCost"] == "CostExplorerEnabled"
    assert conditional["CacheBilledCost"] == "CostExplorerEnabled"
    assert conditional["AppendAuditRows"] == "ActionsEnabled"
    assert "ce:GetCostAndUsage" not in {
        a for item in statements("ActionsRole") for a in as_list(item["Action"])
    }


def test_the_theme_parameters_grant_read_on_one_ssm_parameter_each() -> None:
    reads = [item for item in statements("ReadRole") if item["Action"] == "ssm:GetParameter"]
    assert {item["Sid"] for item in reads} == {"ReadThemeParameter", "ReadLogoParameter"}
    for item in reads:
        assert "parameter" in json.dumps(item["Resource"]) and item["Resource"] != "*"


def test_users_cannot_sign_themselves_up_and_the_client_has_no_secret() -> None:
    pool = resources()["UserPool"]["Properties"]
    assert pool["AdminCreateUserConfig"] == {"AllowAdminCreateUserOnly": True}
    client = resources()["UserPoolClient"]["Properties"]
    assert client["GenerateSecret"] is False
    assert client["AllowedOAuthFlows"] == ["code"]
    assert "implicit" not in json.dumps(client)


def test_the_function_limits_equal_limits_json() -> None:
    function = resources()["DashboardFunction"]["Properties"]
    assert function["Timeout"] == LIMITS["dashboardStackLambdaTimeoutSeconds"]
    assert function["MemorySize"] == LIMITS["dashboardStackLambdaMemoryMib"]


def test_the_actions_group_name_equals_limits_json() -> None:
    assert (
        resources()["ActionsGroup"]["Properties"]["GroupName"]
        == LIMITS["dashboardStackActionsGroup"]
    )


def test_the_function_environment_names_what_the_handler_reads() -> None:
    from_template = set(
        resources()["DashboardFunction"]["Properties"]["Environment"]["Variables"]
    ) - {"AWS_DATA_PATH"}
    config = (REPO_ROOT / "infra/lambdas/dashboard/dashboard_stack/config.py").read_text(
        encoding="utf-8"
    )
    for name in from_template:
        suffix = name.removeprefix("RAYITO_DASHBOARD_")
        assert f'ENV_PREFIX}}{suffix}"' in config, name


def test_the_theme_and_token_numbers_equal_limits_json() -> None:
    parameters = template()["Parameters"]
    cap = LIMITS["dashboardStackThemeParameterMaxBytes"]
    assert parameters["ThemeJson"]["MaxLength"] == cap
    assert f"{cap} bytes" in parameters["ThemeJson"]["Description"]
    assert f"{cap} bytes" in parameters["ThemeParameterName"]["Description"]
    client = resources()["UserPoolClient"]["Properties"]
    assert client["TokenValidityUnits"]["AccessToken"] == "hours"
    assert client["AccessTokenValidity"] == LIMITS["dashboardStackAccessTokenHours"]


def test_the_billed_cost_budget_is_described_with_the_numbers_of_limits_json() -> None:
    description = template()["Parameters"]["EnableCostExplorer"]["Description"]
    assert f"{LIMITS['dashboardStackCostMissesPerDay']} billed requests per UTC day" in description
    assert f"last {LIMITS['dashboardStackCostLookbackMonths']} months" in description


def test_the_advanced_parameter_cost_is_described_with_the_numbers_of_limits_json() -> None:
    parameters = template()["Parameters"]
    price = f"${LIMITS['ssmAdvancedParameterPriceUsdPerMonth']:.2f} per month"
    refresh = f"every {LIMITS['dashboardStackThemeRefreshSeconds']} seconds"
    for name in ("ThemeParameterName", "LogoParameterName"):
        description = " ".join(parameters[name]["Description"].split())
        assert price in description, name
        assert refresh in description, name


def test_the_cache_table_grants_exactly_what_the_cost_cache_and_budget_use() -> None:
    (cache,) = [item for item in statements("ReadRole") if item["Sid"] == "CacheBilledCost"]
    assert set(as_list(cache["Action"])) == {
        "dynamodb:GetItem",
        "dynamodb:PutItem",
        "dynamodb:UpdateItem",
    }
    assert cache["Resource"] == {"Fn::GetAtt": "CacheTable.Arn"}


def test_an_action_checks_the_caller_in_cognito_with_two_reads_on_the_pool_only() -> None:
    conditional = {
        item["Fn::If"][1]["Sid"]: (item["Fn::If"][0], item["Fn::If"][1])
        for policy in resources()["ReadRole"]["Properties"]["Policies"]
        for item in policy["PolicyDocument"]["Statement"]
        if "Fn::If" in item
    }
    condition, check = conditional["CheckCallerBeforeActing"]
    assert condition == "ActionsEnabled"
    assert set(as_list(check["Action"])) == {
        "cognito-idp:AdminGetUser",
        "cognito-idp:AdminListGroupsForUser",
    }
    assert check["Resource"] == {"Fn::GetAtt": "UserPool.Arn"}


def test_the_function_finds_the_spa_client_by_the_name_the_template_gives_it() -> None:
    client_name = resources()["UserPoolClient"]["Properties"]["ClientName"]
    variables = resources()["DashboardFunction"]["Properties"]["Environment"]["Variables"]
    assert variables["RAYITO_DASHBOARD_CLIENT_NAME"] == client_name


PAGE_ORIGIN = {
    "Fn::Join": [
        "",
        [
            "https://",
            {
                "Fn::Select": [
                    2,
                    {"Fn::Split": ["/", {"Fn::GetAtt": "ShellUrl.FunctionUrl"}]},
                ]
            },
        ],
    ]
}
IAM_RESOURCES = frozenset(
    {
        "IdentityPool",
        "IdentityRole",
        "IdentityPoolRoles",
        "IamEndpointParameter",
        "ShellLogGroup",
        "ShellRole",
        "ShellFunction",
        "ShellUrl",
        "ShellUrlInvokePermission",
        "ShellUrlInvokeFunctionPermission",
    }
)


def test_auth_mode_is_an_explicit_parameter_that_defaults_to_the_existing_behaviour() -> (
    None
):
    parameter = template()["Parameters"]["AuthMode"]
    assert parameter["Default"] == "cognito-jwt"
    assert parameter["AllowedValues"] == ["cognito-jwt", "aws-iam"]
    assert template()["Conditions"]["IamAuth"] == {
        "Fn::Equals": [{"Ref": "AuthMode"}, "aws-iam"]
    }


def test_everything_the_iam_variant_adds_exists_only_with_it() -> None:
    found = {
        name
        for name, resource in resources().items()
        if resource.get("Condition") == "IamAuth"
    }
    assert found == IAM_RESOURCES
    assert template()["Outputs"]["ApiUrl"]["Condition"] == "IamAuth"


def test_the_api_url_with_iam_names_only_the_page_in_cors_and_never_allows_credentials() -> (
    None
):
    _, cors, _ = resources()["FunctionUrl"]["Properties"]["Cors"]["Fn::If"]
    assert cors["AllowOrigins"] == [PAGE_ORIGIN]
    assert cors["MaxAge"] == LIMITS["dashboardStackApiCorsMaxAgeSeconds"]
    assert "AllowCredentials" not in cors
    assert set(cors["AllowMethods"]) == {"GET", "POST"}
    assert set(cors["AllowHeaders"]) == {
        "authorization",
        "content-type",
        "x-amz-content-sha256",
        "x-amz-date",
        "x-amz-security-token",
        "x-rayito-token",
    }


def test_the_api_function_gets_no_public_permission_with_iam() -> None:
    for name in ("UrlInvokePermission", "UrlInvokeFunctionPermission"):
        assert resources()[name]["Condition"] == "JwtAuth"
        assert resources()[name]["Properties"]["FunctionName"] == {
            "Ref": "DashboardFunction"
        }
    for name in ("ShellUrlInvokePermission", "ShellUrlInvokeFunctionPermission"):
        assert resources()[name]["Properties"]["FunctionName"] == {
            "Ref": "ShellFunction"
        }


def test_the_page_url_is_public_and_is_the_cognito_callback_with_iam() -> None:
    assert resources()["ShellUrl"]["Properties"]["AuthType"] == "NONE"
    assert "Cors" not in resources()["ShellUrl"]["Properties"]
    callbacks = resources()["UserPoolClient"]["Properties"]["CallbackURLs"]
    assert callbacks == [
        {
            "Fn::If": [
                "IamAuth",
                {"Fn::GetAtt": "ShellUrl.FunctionUrl"},
                {"Fn::GetAtt": "FunctionUrl.FunctionUrl"},
            ]
        }
    ]
    outputs = template()["Outputs"]["DashboardUrl"]["Value"]["Fn::If"]
    assert outputs[1] == {"Fn::GetAtt": "ShellUrl.FunctionUrl"}


def test_the_page_function_can_do_nothing_but_serve_the_page() -> None:
    assert allowed_actions("ShellRole") == {
        "logs:CreateLogStream",
        "logs:PutLogEvents",
        "cognito-idp:ListUserPoolClients",
        "ssm:GetParameter",
    }
    for item in statements("ShellRole"):
        assert item["Resource"] != "*"
    function = resources()["ShellFunction"]["Properties"]
    assert function["Role"] == {"Fn::GetAtt": "ShellRole.Arn"}
    assert (
        function["Environment"]["Variables"]["RAYITO_DASHBOARD_SURFACE"] == "iam-shell"
    )
    assert resources()["DashboardFunction"]["Properties"]["Environment"]["Variables"][
        "RAYITO_DASHBOARD_SURFACE"
    ] == {"Fn::If": ["IamAuth", "iam-api", "full"]}


def test_the_page_function_limits_equal_limits_json() -> None:
    function = resources()["ShellFunction"]["Properties"]
    assert function["Timeout"] == LIMITS["dashboardStackShellLambdaTimeoutSeconds"]
    assert function["MemorySize"] == LIMITS["dashboardStackShellLambdaMemoryMib"]


def test_the_page_function_environment_names_what_its_handler_reads() -> None:
    names = set(resources()["ShellFunction"]["Properties"]["Environment"]["Variables"])
    config = (
        REPO_ROOT / "infra/lambdas/dashboard/dashboard_stack/config.py"
    ).read_text(encoding="utf-8")
    for name in names:
        assert f'ENV_PREFIX}}{name.removeprefix("RAYITO_DASHBOARD_")}"' in config, name


def test_the_identity_pool_refuses_unauthenticated_identities_and_trusts_only_the_client() -> (
    None
):
    pool = resources()["IdentityPool"]["Properties"]
    assert pool["AllowUnauthenticatedIdentities"] is False
    (provider,) = pool["CognitoIdentityProviders"]
    assert provider["ClientId"] == {"Ref": "UserPoolClient"}
    assert provider["ServerSideTokenCheck"] is True
    roles = resources()["IdentityPoolRoles"]["Properties"]
    assert set(roles["Roles"]) == {"authenticated"}


def test_the_authenticated_role_can_only_invoke_the_api_function_through_its_url() -> (
    None
):
    role = resources()["IdentityRole"]
    (trust,) = role["Properties"]["AssumeRolePolicyDocument"]["Statement"]
    assert trust["Principal"] == {"Federated": "cognito-identity.amazonaws.com"}
    assert trust["Action"] == "sts:AssumeRoleWithWebIdentity"
    assert trust["Condition"]["StringEquals"]["cognito-identity.amazonaws.com:aud"] == {
        "Ref": "IdentityPool"
    }
    assert trust["Condition"]["ForAnyValue:StringLike"] == {
        "cognito-identity.amazonaws.com:amr": "authenticated"
    }
    assert allowed_actions("IdentityRole") == {
        "lambda:InvokeFunctionUrl",
        "lambda:InvokeFunction",
    }
    for item in statements("IdentityRole"):
        assert item["Resource"] == {"Fn::GetAtt": "DashboardFunction.Arn"}
        assert item["Condition"]
    url_statement = next(
        item
        for item in statements("IdentityRole")
        if item["Action"] == "lambda:InvokeFunctionUrl"
    )
    assert url_statement["Condition"] == {
        "StringEquals": {"lambda:FunctionUrlAuthType": "AWS_IAM"}
    }


def test_the_endpoint_parameter_holds_exactly_the_pool_and_the_api_address() -> None:
    parameter = resources()["IamEndpointParameter"]["Properties"]
    value = json.loads(
        parameter["Value"]["Fn::Sub"]
        .replace("${IdentityPool}", "pool")
        .replace("${FunctionUrl.FunctionUrl}", "url")
    )
    assert value == {"identityPoolId": "pool", "apiUrl": "url"}
    shell = resources()["ShellFunction"]["Properties"]["Environment"]["Variables"]
    assert shell["RAYITO_DASHBOARD_IDENTITY_PARAMETER"] == {
        "Fn::Sub": "/rayito/dashboard/${AWS::StackName}/iam-endpoint"
    }
    assert parameter["Name"] == shell["RAYITO_DASHBOARD_IDENTITY_PARAMETER"]
