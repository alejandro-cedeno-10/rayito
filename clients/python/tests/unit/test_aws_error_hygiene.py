"""Los caminos que antes se saltaban `sanitize_aws_error`/`redact_aws_text`
(stacks, `Template.build`, el manejador de errores de la CLI, `doctor` y
`prune`): un `InvalidSignatureException` cuyo `Message` trae la cadena
canónica con `x-amz-security-token` no deja el token ni el id de clave en
`str()`, en `__cause__`, en un traceback ni en el stderr de la CLI."""

from __future__ import annotations

import traceback
from typing import Any

import boto3
import pytest
from botocore.exceptions import ClientError
from botocore.stub import Stubber

from rayito._stacks._cloudformation import CloudFormationProvisioner
from rayito._templates._build import submit_build
from rayito.cli._console import client_error_message, translated_failures
from rayito.exceptions import BuildException, StackException

SESSION_TOKEN = "IQoJb3JpZ2luX2VjEXAMPLESESSIONTOKENVALUE0123456789"
ACCESS_KEY_ID = "ASIAFAKEKEYIDEXAMPLE"
INVALID_SIGNATURE_MESSAGE = (
    "The request signature we calculated does not match the signature you provided."
    "\n\nThe Canonical String for this request should have been\n"
    f"'POST\n/\n\nhost:example.com\nx-amz-security-token:{SESSION_TOKEN}\n'"
    "\n\nThe String-to-Sign should have been\n'AWS4-HMAC-SHA256\n"
    f"20261004T000000Z\n20261004/us-east-1/lambda/aws4_request {ACCESS_KEY_ID}'\n"
)
LEAKS = (SESSION_TOKEN, ACCESS_KEY_ID)


def signature_error(operation: str = "CreateMicrovmImage") -> ClientError:
    return ClientError(
        {
            "Error": {"Code": "InvalidSignatureException", "Message": INVALID_SIGNATURE_MESSAGE},
            "ResponseMetadata": {"HTTPStatusCode": 403, "RequestId": "req-1"},
        },
        operation,
    )


def rendered(exc: BaseException) -> str:
    """Todo lo que imprimiría un traceback, `__cause__` y `__context__` incluidos."""
    return "".join(traceback.format_exception(exc))


def assert_clean(text: str) -> None:
    for leak in LEAKS:
        assert leak not in text


class _FrozenLazyClient:
    def __init__(self, client: object) -> None:
        self._client = client

    def get(self) -> object:
        return self._client


def test_stack_errors_carry_a_sanitized_cause() -> None:
    client = boto3.client("cloudformation", region_name="us-east-1")
    provisioner = CloudFormationProvisioner.__new__(CloudFormationProvisioner)
    provisioner._cloudformation = _FrozenLazyClient(client)
    with Stubber(client) as stubber:
        stubber.add_client_error(
            "describe_stacks",
            service_error_code="InvalidSignatureException",
            service_message=INVALID_SIGNATURE_MESSAGE,
            http_status_code=403,
        )
        with pytest.raises(StackException) as excinfo:
            provisioner.describe("rayito-metadata-index")
    assert "InvalidSignatureException" in str(excinfo.value)
    assert not isinstance(excinfo.value.__cause__, ClientError)
    assert_clean(rendered(excinfo.value))


class _SubmitClients:
    """Lo mínimo de `BuildClients` para `submit_build`: la imagen no
    existe y `create-microvm-image` falla con un error de firma."""

    region = "us-east-1"
    account_id = "123456789012"

    @property
    def microvms(self) -> Any:
        return self

    def get_microvm_image(self, **_kwargs: Any) -> dict[str, Any]:
        raise ClientError(
            {"Error": {"Code": "ResourceNotFoundException", "Message": "no"}}, "GetMicrovmImage"
        )

    def create_microvm_image(self, **_kwargs: Any) -> dict[str, Any]:
        raise signature_error()


def test_a_rejected_template_build_is_a_build_exception_with_a_sanitized_cause() -> None:
    with pytest.raises(BuildException) as excinfo:
        submit_build(_SubmitClients(), "tpl", "arn:aws:lambda:us-east-1:123456789012:x", {})  # type: ignore[arg-type]
    assert excinfo.value.reason == "aws_error"
    assert "InvalidSignatureException" in str(excinfo.value)
    assert not isinstance(excinfo.value.__cause__, ClientError)
    assert_clean(rendered(excinfo.value))


def test_client_error_message_is_redacted() -> None:
    """`doctor` y `prune` muestran `client_error_message` tal cual."""
    message = client_error_message(signature_error())
    assert message.startswith("The request signature we calculated")
    assert_clean(message)


def test_the_cli_top_level_handler_redacts_aws_messages(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit), translated_failures():
        raise signature_error()
    err = capsys.readouterr().err
    assert "AWS error InvalidSignatureException" in err
    assert_clean(err)
