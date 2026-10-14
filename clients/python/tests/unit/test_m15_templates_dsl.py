"""`Template`/`AsyncTemplate` (m15-templates): el builder es inmutable, las
instrucciones de cable se compilan en orden, y lo que 0.6 no soporta
lanza `UnimplementedError`/`InvalidArgumentException` antes de cualquier
E/S."""

from __future__ import annotations

import pytest

from rayito import AsyncTemplate, Template, wait_for_port
from rayito._templates._dsl import NODE_PACKAGES, NPM_MISSING_EXIT_CODE
from rayito._templates._instructions import (
    BASE_IMAGE_KIND,
    CopyStep,
    EnvStep,
    RunStep,
    UserStep,
)
from rayito.exceptions import InvalidArgumentException, UnimplementedError


def test_the_builder_never_mutates_a_previous_instance() -> None:
    base = Template()
    with_copy = base.copy("app/", "/srv/app/")
    assert base.spec.steps == ()
    assert with_copy.spec.steps == (CopyStep("app/", "/srv/app/"),)


def test_from_base_image_records_the_base_without_any_io() -> None:
    t = Template().from_base_image("rayito-base")
    assert t.spec.base is not None
    assert t.spec.base.kind == BASE_IMAGE_KIND
    assert t.spec.base.name == "rayito-base"
    assert t.spec.base.version is None


def test_steps_compile_in_call_order() -> None:
    t = (
        Template()
        .from_base_image()
        .pip_install(["pandas==2.2.3"])
        .copy("app/", "/srv/app/")
        .set_envs({"MODE": "prod"})
    )
    assert t.spec.steps == (
        RunStep("python3 -m pip install --no-cache-dir --break-system-packages pandas==2.2.3"),
        CopyStep("app/", "/srv/app/"),
        EnvStep("MODE", "prod"),
    )


@pytest.mark.parametrize(
    "method", ["from_image", "from_template", "from_dockerfile", "from_gcp_registry"]
)
def test_unsupported_base_sources_raise_before_any_io(method: str) -> None:
    with pytest.raises(UnimplementedError):
        getattr(Template(), method)("whatever")


def test_apt_install_names_dnf_install_instead() -> None:
    with pytest.raises(UnimplementedError, match="dnf_install"):
        Template().apt_install("curl")


def test_dnf_install_generates_a_quoted_dnf_step() -> None:
    t = Template().from_base_image().dnf_install(["tree", "jq"])
    assert t.spec.steps == (
        RunStep("dnf install -y --setopt=install_weak_deps=0 tree jq && dnf clean all"),
    )


def test_dnf_install_accepts_a_single_name_and_quotes_shell_metacharacters() -> None:
    t = Template().dnf_install("tree; rm -rf /")
    assert t.spec.steps == (
        RunStep("dnf install -y --setopt=install_weak_deps=0 'tree; rm -rf /' && dnf clean all"),
    )


def test_npm_install_guards_on_npm_and_supports_global() -> None:
    local = Template().npm_install(["left-pad@1.3.0"])
    scoped = Template().npm_install("@scope/pkg", g=True)
    (local_step,) = local.spec.steps
    (scoped_step,) = scoped.spec.steps
    assert isinstance(local_step, RunStep)
    assert isinstance(scoped_step, RunStep)
    assert local_step.cmd.startswith("command -v npm >/dev/null 2>&1 || { echo ")
    assert (
        f"exit {NPM_MISSING_EXIT_CODE}; }} && npm install --no-audit --no-fund left-pad@1.3.0"
        in local_step.cmd
    )
    assert scoped_step.cmd.endswith("npm install --no-audit --no-fund -g @scope/pkg")
    assert "dnf_install" in local_step.cmd


def test_npm_install_missing_message_names_the_measured_node_packages() -> None:
    (step,) = Template().npm_install("left-pad").spec.steps
    assert isinstance(step, RunStep)
    assert NODE_PACKAGES == ("nodejs20", "nodejs20-npm")
    assert '["nodejs20", "nodejs20-npm"]' in step.cmd


def test_npm_install_without_packages_runs_a_plain_install_in_the_workdir() -> None:
    (step,) = Template().npm_install().spec.steps
    assert isinstance(step, RunStep)
    assert step.cmd.endswith("&& npm install --no-audit --no-fund")


def test_npm_install_accepts_g_positionally_like_e2b() -> None:
    (step,) = Template().npm_install("typescript", True).spec.steps
    assert isinstance(step, RunStep)
    assert step.cmd.endswith("npm install --no-audit --no-fund -g typescript")


def test_dnf_install_runs_as_root_and_restores_a_non_root_user() -> None:
    t = Template().set_user("user").dnf_install("tree")
    steps = t.spec.steps
    assert steps[0] == UserStep("user")
    assert steps[1] == UserStep("root")
    assert isinstance(steps[2], RunStep)
    assert steps[3] == UserStep("user")
    assert len(steps) == 4


@pytest.mark.parametrize("user", [None, "root", "0"])
def test_dnf_install_does_not_wrap_when_already_root(user: str | None) -> None:
    base = Template() if user is None else Template().set_user(user)
    steps = base.dnf_install("tree").spec.steps
    assert [type(step) for step in steps].count(UserStep) == (0 if user is None else 1)
    assert isinstance(steps[-1], RunStep)


def test_dnf_install_wraps_around_the_latest_user_only() -> None:
    steps = Template().set_user("a").set_user("b").dnf_install("tree").spec.steps
    assert steps[-3:] == (UserStep("root"), steps[-2], UserStep("b"))


def test_npm_install_global_runs_as_root_but_local_keeps_the_user() -> None:
    base = Template().set_user("user")
    global_steps = base.npm_install("typescript", g=True).spec.steps
    local_steps = base.npm_install("left-pad").spec.steps
    assert global_steps[1] == UserStep("root")
    assert global_steps[-1] == UserStep("user")
    assert [type(step) for step in local_steps] == [UserStep, RunStep]


@pytest.mark.parametrize("call", ["dnf_install", "npm_install"])
@pytest.mark.parametrize("packages", [[], [" "], [""], ["--help"], "-y"])
def test_install_shortcuts_reject_empty_and_option_like_names(
    call: str, packages: str | list[str]
) -> None:
    with pytest.raises(InvalidArgumentException):
        getattr(Template(), call)(packages)


def test_install_shortcuts_exist_on_the_async_builder() -> None:
    t = AsyncTemplate().from_base_image().dnf_install("tree").npm_install("left-pad")
    assert isinstance(t, AsyncTemplate)
    assert len(t.spec.steps) == 2


@pytest.mark.parametrize(
    ("call", "args"),
    [
        ("copy", ("", "/dst")),
        ("copy", ("src", "relative")),
        ("run_cmd", ("   ",)),
        ("pip_install", ([],)),
        ("workdir", ("relative",)),
        ("set_user", ("",)),
        ("set_start_cmd", ("   ",)),
    ],
)
def test_invalid_inputs_raise_before_any_io(call: str, args: tuple[str, ...]) -> None:
    with pytest.raises(InvalidArgumentException):
        getattr(Template(), call)(*args)


def test_set_start_cmd_accepts_a_ready_command_a_string_or_nothing() -> None:
    with_ready_command = Template().set_start_cmd("run.sh", wait_for_port(8000))
    assert with_ready_command.spec.start is not None
    assert with_ready_command.spec.start.ready_cmd is not None
    assert with_ready_command.spec.start.ready_poll is not None

    with_raw_string = Template().set_start_cmd("run.sh", "test -e /tmp/ready")
    assert with_raw_string.spec.start is not None
    assert with_raw_string.spec.start.ready_cmd == "test -e /tmp/ready"
    assert with_raw_string.spec.start.ready_poll is None

    with_nothing = Template().set_start_cmd("run.sh")
    assert with_nothing.spec.start is not None
    assert with_nothing.spec.start.ready_cmd is None


def test_skip_cache_is_a_pure_flag() -> None:
    assert Template().spec.skip_cache is False
    assert Template().skip_cache().spec.skip_cache is True
    assert Template().skip_cache(skip_cache=False).spec.skip_cache is False


def test_async_template_is_a_template_subclass_with_the_same_fluent_surface() -> None:
    t = AsyncTemplate().from_base_image().pip_install("pandas")
    assert isinstance(t, Template)
    assert isinstance(t, AsyncTemplate)


def test_to_json_is_stable_for_the_same_spec() -> None:
    t = Template().from_base_image().pip_install("pandas")
    assert t.to_json() == t.to_json()


def test_constructing_the_builder_opens_no_aws_client(monkeypatch: pytest.MonkeyPatch) -> None:
    import boto3

    def _forbidden(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("Template() no debe construir ningún cliente boto3")

    monkeypatch.setattr(boto3.session.Session, "client", _forbidden)
    (
        Template()
        .from_base_image("rayito-base")
        .pip_install(["pandas"])
        .copy("app/", "/srv/app/")
        .set_envs({"MODE": "prod"})
        .set_start_cmd("python -m http.server 8000", wait_for_port(8000))
        .to_dockerfile()
    )
