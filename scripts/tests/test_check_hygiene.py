"""``check_hygiene.py`` over crafted lines and temporary trees: every rule
reports a real-looking sample and lets its documented placeholder through,
one line reports each rule once, ``main`` prints ``path:line`` without the
line itself and exits 1, binary and untracked files are skipped, and the real
repository is clean. The positive samples are assembled from pieces, so this
file passes the gate it tests."""

from __future__ import annotations

import io
import subprocess
import sys
from contextlib import redirect_stdout
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1]
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import check_hygiene

REPO_ROOT = SCRIPTS.parent
ACCOUNT = "210987654321"
UUID = "5f2d7c1e-9a4b-3c6d-8e1f-2a3b4c5d6e7f"
HEX17 = "0a1b2c3d4e5f67890"
HEX8 = "1a2b3c4d"
SSO_SUFFIX = "ab12cd34ef56ab78"
DIGIT = "1"
KEY_BODY = "Q2W3E4R5T6Y7U8I9"
DRIVE = "C"
OTHER_DRIVE = "D"
GIT_BASH_DRIVE = "d"
GIT_BASH_SYSTEM_DRIVE = "c"
QUALIFIER = check_hygiene.CDK_QUALIFIER


def scan(tmp_path: Path, name: str, content: bytes) -> tuple[int, str]:
    path = tmp_path / name
    path.write_bytes(content)
    stdout = io.StringIO()
    with redirect_stdout(stdout):
        code = check_hygiene.main([str(path)], root=tmp_path)
    return code, stdout.getvalue()


def git(root: Path, *arguments: str) -> None:
    subprocess.run(
        ["git", "-C", str(root), *arguments], check=True, capture_output=True
    )


@pytest.mark.parametrize(
    "line",
    [
        f"arn:aws:iam::{ACCOUNT}:role/deployer",
        f'"template": "arn:aws:lambda:us-east-1:{ACCOUNT}:microvm-image:rayito-base"',
        f"arn:aws-us-gov:sts::{ACCOUNT}:assumed-role/admin/session",
    ],
)
def test_real_account_in_an_arn_is_reported(line: str) -> None:
    assert check_hygiene.line_reasons(line) == [check_hygiene.ACCOUNT_IN_ARN_REASON]


@pytest.mark.parametrize(
    "line",
    [
        "arn:aws:iam::123456789012:role/rayito-m0-execution-us-east-1",
        "arn:aws:iam::000000000000:root",
        "arn:aws:iam::111122223333:role/x",
        "arn:aws:iam::444455556666:role/x",
        "arn:aws:lambda:us-east-1:aws:microvm-image:al2023-1",
        "arn:aws:lambda:us-east-1:aws:network-connector:aws-network-connector:ALL_INGRESS",
        "arn:aws:s3:::amzn-s3-demo-bucket/rayito/*",
    ],
)
def test_placeholder_accounts_and_managed_arns_pass(line: str) -> None:
    assert check_hygiene.line_reasons(line) == []


@pytest.mark.parametrize(
    "line",
    [
        f"uploaded s3://artifacts-{ACCOUNT}/rayito/images/rayd.zip",
        f"arn:aws:s3:::logs-{ACCOUNT}-archive/*",
        f"--bucket my-assets-{ACCOUNT}-eu-west-1",
        f"{ACCOUNT}.dkr.ecr.us-east-1.amazonaws.com/rayito:1",
    ],
)
def test_real_account_in_a_bucket_or_registry_name_is_reported(line: str) -> None:
    assert check_hygiene.line_reasons(line) == [check_hygiene.ACCOUNT_IN_NAME_REASON]


@pytest.mark.parametrize(
    "line",
    [
        "--bucket amzn-s3-demo-bucket",
        'BucketName::parse("amzn-s3-demo-bucket-123456789012-us-east-1")',
        "s3://b/rayito/images/rayd-000000000000.zip",
        '"timestamp_unix_ns":1789000000000000000',
    ],
)
def test_placeholder_bucket_names_pass(line: str) -> None:
    assert check_hygiene.line_reasons(line) == []


@pytest.mark.parametrize(
    "line",
    [
        f"cdk-{QUALIFIER}-assets-123456789012-us-east-1",
        f"cdk-{QUALIFIER}-container-assets",
    ],
)
def test_cdk_bootstrap_bucket_is_reported(line: str) -> None:
    assert check_hygiene.line_reasons(line) == [check_hygiene.CDK_BUCKET_REASON]


@pytest.mark.parametrize(
    "line", ["cdk.json", "npx cdk bootstrap", "cdk-assets publish"]
)
def test_other_cdk_mentions_pass(line: str) -> None:
    assert check_hygiene.line_reasons(line) == []


@pytest.mark.parametrize(
    "line",
    [
        f"AWS_PROFILE=AdministratorAccess-{ACCOUNT} AWS_REGION=us-east-1",
        f"aws sso login --profile AdministratorAccess-{DIGIT}",
        f"--profile PowerUserAccess-{ACCOUNT}",
        f"role `AWSReservedSSO_AdministratorAccess_{SSO_SUFFIX}`",
    ],
)
def test_sso_profile_or_role_with_account_data_is_reported(line: str) -> None:
    assert check_hygiene.line_reasons(line) == [check_hygiene.SSO_REASON]


@pytest.mark.parametrize(
    "line",
    [
        "AWS_PROFILE=<tu-perfil>",
        "the managed policy AdministratorAccess",
        "arn:aws:iam::123456789012:role/aws-reserved/sso.amazonaws.com/AWSReservedSSO_Admin_abc",
        "AWSReservedSSO_AdministratorAccess_<suffix>",
    ],
)
def test_sso_placeholders_pass(line: str) -> None:
    assert check_hygiene.line_reasons(line) == []


@pytest.mark.parametrize(
    "line",
    [
        f'SANDBOX_ID = "microvm-{UUID}"',
        f"`2026/09/15[1.0]microvm-{UUID[:8]}-…`",
        f"`microvm-{UUID[:8]}...: al cerrar",
        f'"endpoint": "{UUID}.lambda-microvm.us-east-1.on.aws"',
    ],
)
def test_real_microvm_id_or_endpoint_is_reported(line: str) -> None:
    assert check_hygiene.line_reasons(line) == [check_hygiene.MICROVM_REASON]


@pytest.mark.parametrize(
    "line",
    [
        '"microvm_id": "microvm-00000000-0000-0000-0000-000000000001"',
        '"microvm_id": "microvm-00000000-0000-0000-0000-000000000142"',
        "`microvm-00000000-0000-0000-0000-<12 dígitos>`",
        "sandbox de `--launch` `microvm-<id>` TERMINATING",
        "prefijo **`microvm-<uuid>`**",
        "arn:aws:lambda:us-east-1:123456789012:microvm-image:rayito-base",
        '"endpoint": "00000000-0000-0000-0000-000000000002.lambda-microvm.us-east-1.on.aws"',
        "endpoint = <id>.lambda-microvm.<region>.on.aws",
    ],
)
def test_fake_microvm_ids_and_placeholders_pass(line: str) -> None:
    assert check_hygiene.line_reasons(line) == []


@pytest.mark.parametrize(
    "line",
    [
        f"la única VPC (`vpc-{HEX17}`)",
        f"SubnetIds=subnet-{HEX8}",
        f"SecurityGroupIds=sg-{HEX17}",
        f"eni-{HEX17} attached",
    ],
)
def test_real_network_id_is_reported(line: str) -> None:
    assert check_hygiene.line_reasons(line) == [check_hygiene.NETWORK_REASON]


@pytest.mark.parametrize(
    "line",
    [
        "VpcId=vpc-0123456789abcdef0 SubnetIds=subnet-0123456789abcdef0",
        "SecurityGroupIds=sg-0123456789abcdef0",
        f"msg-{HEX8}",
        "VpcId=… SubnetIds=…",
    ],
)
def test_placeholder_network_ids_pass(line: str) -> None:
    assert check_hygiene.line_reasons(line) == []


@pytest.mark.parametrize(
    "line",
    [
        f'File "{DRIVE}:\\Users\\someone\\repo\\run.py", line 1',
        f"{DRIVE}:/Users/someone/AppData",
        f'"cwd": "{DRIVE}:\\\\Users\\\\someone"',
        f"export RUSTUP_HOME={OTHER_DRIVE}:/tools/rustup",
        f"done: results in {OTHER_DRIVE}:\\Projects\\repo\\out.jsonl",
        f'export PATH="/{GIT_BASH_DRIVE}/tools/bin:$PATH"',
        f"cd /{GIT_BASH_SYSTEM_DRIVE}/Users/someone",
        f"/{GIT_BASH_DRIVE}/Projects/repo",
    ],
)
def test_local_machine_path_is_reported(line: str) -> None:
    assert check_hygiene.line_reasons(line) == [check_hygiene.LOCAL_PATH_REASON]


@pytest.mark.parametrize(
    "line",
    [
        "LogGroupPrefix=C:/Program Files/Git/rayito",
        "cd /home/user/work",
        "https://example.com/a/Users/list",
        'export PATH="$CARGO_HOME/bin:<dir-zig>:$PATH"',
        "clients/python/tests/unit/cli/fixtures/quotas.json",
    ],
)
def test_repo_relative_and_generic_paths_pass(line: str) -> None:
    assert check_hygiene.line_reasons(line) == []


@pytest.mark.parametrize(
    "line",
    [f"AKIA{KEY_BODY}", f"aws_access_key_id = ASIA{KEY_BODY}"],
)
def test_access_key_id_is_reported(line: str) -> None:
    assert check_hygiene.line_reasons(line) == [check_hygiene.ACCESS_KEY_REASON]


@pytest.mark.parametrize("line", ["AKIAIOSFODNN7EXAMPLE", "AKIA1234", "ASIA"])
def test_documentation_key_and_short_prefixes_pass(line: str) -> None:
    assert check_hygiene.line_reasons(line) == []


def test_a_line_reports_each_rule_once() -> None:
    line = f"microvm-{UUID} microvm-{UUID[:8]} arn:aws:iam::{ACCOUNT}:root"

    assert check_hygiene.line_reasons(line) == [
        check_hygiene.ACCOUNT_IN_ARN_REASON,
        check_hygiene.MICROVM_REASON,
    ]


def test_main_prints_path_and_line_but_never_the_line(tmp_path: Path) -> None:
    content = (
        f"clean\naws_access_key_id = AKIA{KEY_BODY}\narn:aws:iam::{ACCOUNT}:root\n"
    )

    code, printed = scan(tmp_path, "leak.txt", content.encode("utf-8"))

    assert code == 1
    assert f"leak.txt:2: {check_hygiene.ACCESS_KEY_REASON}" in printed
    assert f"leak.txt:3: {check_hygiene.ACCOUNT_IN_ARN_REASON}" in printed
    assert KEY_BODY not in printed
    assert ACCOUNT not in printed
    assert "KO 2 hallazgo(s)" in printed


def test_main_accepts_a_clean_file(tmp_path: Path) -> None:
    code, printed = scan(tmp_path, "clean.md", b"arn:aws:iam::123456789012:root\n")

    assert code == 0
    assert "OK 1 fichero(s)" in printed


@pytest.mark.parametrize(
    "content",
    [
        b"\x00\x01" + f"AKIA{KEY_BODY}".encode("ascii"),
        b"\xff\xfe" + f"AKIA{KEY_BODY}".encode("ascii"),
    ],
)
def test_binary_files_are_skipped(tmp_path: Path, content: bytes) -> None:
    code, printed = scan(tmp_path, "blob.bin", content)

    assert code == 0
    assert "OK 0 fichero(s)" in printed


def test_only_tracked_files_are_scanned(tmp_path: Path) -> None:
    git(tmp_path, "init", "-q")
    (tmp_path / "tracked.md").write_text("microvm-<id>\n", encoding="utf-8")
    (tmp_path / "untracked.log").write_text(f"microvm-{UUID}\n", encoding="utf-8")
    git(tmp_path, "add", "tracked.md")

    stdout = io.StringIO()
    with redirect_stdout(stdout):
        untracked_ignored = check_hygiene.main([], root=tmp_path)
    git(tmp_path, "add", "untracked.log")
    with redirect_stdout(stdout):
        tracked_reported = check_hygiene.main([], root=tmp_path)

    assert untracked_ignored == 0
    assert tracked_reported == 1
    assert "untracked.log:1:" in stdout.getvalue()


def test_the_gate_and_its_tests_pass_themselves() -> None:
    stdout = io.StringIO()
    with redirect_stdout(stdout):
        code = check_hygiene.main(
            [str(SCRIPTS / "check_hygiene.py"), str(Path(__file__).resolve())],
            root=REPO_ROOT,
        )

    assert code == 0, stdout.getvalue()


def test_the_repository_itself_is_clean() -> None:
    stdout = io.StringIO()
    with redirect_stdout(stdout):
        code = check_hygiene.main([], root=REPO_ROOT)

    assert code == 0, stdout.getvalue()
