"""`secret_env` y `secret_env_from` (ADR-033 D4): los vectores compartidos con
TypeScript y el host (`testdata/agent-secret-env/`), las banderas, las tablas
del fichero de proyecto, los nombres reservados, la especificación de
`RAYITO_SECRET_ENV`, la expansión, el lector de `.env` y los nombres de
`.env.example`. Todo es puro: ninguna prueba crea un cliente de AWS."""

from __future__ import annotations

import json
import tomllib
from pathlib import Path
from typing import Any

import pytest

from rayito import SecretEnvRef, SecretEnvSource
from rayito._agent._detect_env_names import (
    env_missing_warnings,
    missing_env_names,
    required_env_names,
)
from rayito._agent._project_domain import AgentProject, AgentProjectException
from rayito._agent._runner.agent_host import secret_env as host
from rayito._agent._secret_env_domain import (
    dotenv_names,
    is_reserved_name,
    parse_dotenv,
    reference_of,
    resolve_secret_env,
    secret_env_names,
    secret_ids,
    spec_document,
    spec_variable,
)
from rayito._agent._secret_env_project import (
    parse_secret_env_flag,
    parse_secret_env_flags,
    parse_secret_env_from_flag,
    parse_secret_env_from_flags,
    with_secret_env,
)
from rayito._limits import (
    AGENT_SECRET_ENV_HELPER_TIMEOUT_SECONDS,
    AGENT_SECRET_ENV_MAX_SECRETS,
    AGENT_SECRET_ENV_MAX_TOTAL_BYTES,
    AGENT_SECRET_ENV_RESERVED_NAMES,
    AGENT_SECRET_ENV_RESERVED_PREFIXES,
    AGENT_SECRET_ENV_SPEC_VERSION,
    AGENT_SECRET_ENV_VARIABLE,
)
from rayito.exceptions import AgentException, InvalidArgumentException
from tests.unit.fake_agent_project import project

REPO = Path(__file__).resolve().parents[4]
DATA = REPO / "testdata" / "agent-secret-env"
PARSE = json.loads((DATA / "parse.json").read_text(encoding="utf-8"))
EXPAND = json.loads((DATA / "expand.json").read_text(encoding="utf-8"))
SPEC = json.loads((DATA / "spec.json").read_text(encoding="utf-8"))
RESERVED = json.loads((DATA / "reserved.json").read_text(encoding="utf-8"))
DOTENV = json.loads((DATA / "dotenv.json").read_text(encoding="utf-8"))
ENV_NAMES = json.loads((DATA / "env-names.json").read_text(encoding="utf-8"))
TOO_MANY_BYTES = AGENT_SECRET_ENV_MAX_TOTAL_BYTES + 1
MINIMAL = (REPO / "testdata" / "agent-project" / "valid" / "minimal.toml").read_text("utf-8")


def _reference_document(value: str | SecretEnvRef) -> dict[str, str]:
    reference = reference_of(value)
    document = {"secret": reference.secret}
    if reference.key is not None:
        document["key"] = reference.key
    return document


def _source_document(source: SecretEnvSource) -> dict[str, Any]:
    return {
        "secret": source.secret,
        "prefix": source.prefix,
        "keys": None if source.keys is None else list(source.keys),
    }


def _project_values(document: dict[str, Any]) -> tuple[dict[str, Any], list[SecretEnvSource]]:
    secret_env = {
        name: value if isinstance(value, str) else SecretEnvRef(**value)
        for name, value in document["secret_env"].items()
    }
    sources = [
        SecretEnvSource(
            item["secret"],
            prefix=item.get("prefix", ""),
            keys=None if item.get("keys") is None else tuple(item["keys"]),
        )
        for item in document["secret_env_from"]
    ]
    return secret_env, sources


# ------------------------------------------------------------------ parse.json


@pytest.mark.parametrize(
    "case", PARSE["flagCases"], ids=[f"{c['flag']} {c['value']}" for c in PARSE["flagCases"]]
)
def test_the_flag_vectors(case: dict[str, Any]) -> None:
    def parse() -> Any:
        if case["flag"] == "--secret-env":
            name, value = parse_secret_env_flag(case["value"])
            return {"name": name, "ref": _reference_document(value)}
        return _source_document(parse_secret_env_from_flag(case["value"], 0))

    if "error" in case:
        with pytest.raises(AgentProjectException) as raised:
            parse()
        assert {"code": raised.value.code, "path": raised.value.path} == case["error"]
        return
    assert parse() == case["expected"]


def test_a_flag_error_never_repeats_the_pasted_value() -> None:
    with pytest.raises(AgentProjectException) as raised:
        parse_secret_env_flag("OPENAI_API_KEY=sk-not-a-name")
    assert "sk-not-a-name" not in str(raised.value)


@pytest.mark.parametrize("case", PARSE["tableCases"], ids=[c["name"] for c in PARSE["tableCases"]])
def test_the_table_vectors(case: dict[str, Any]) -> None:
    document = tomllib.loads(MINIMAL + "\n" + case["toml"])
    if "error" in case:
        with pytest.raises(AgentProjectException) as raised:
            AgentProject.from_dict(document, base_dir=".")
        assert {"code": raised.value.code, "path": raised.value.path} == case["error"]
        return
    canonical = AgentProject.from_dict(document, base_dir=".").to_dict()
    assert {key: canonical[key] for key in case["expected"]} == case["expected"]


def test_a_project_without_secret_env_has_no_new_keys() -> None:
    canonical = project().to_dict()
    assert "secret_env" not in canonical
    assert "secret_env_from" not in canonical


def test_flags_add_to_the_projects_secret_env() -> None:
    base = project(secret_env={"DATABASE_URL": "app/db-url"})
    changed = with_secret_env(
        base,
        parse_secret_env_flags(["LLM_GATEWAY_KEY=llm#api_key"]),
        parse_secret_env_from_flags(["app/config,prefix=APP_,keys=DB_HOST"]),
    )
    assert changed.secret_env == {
        "DATABASE_URL": "app/db-url",
        "LLM_GATEWAY_KEY": SecretEnvRef("llm", key="api_key"),
    }
    assert changed.secret_env_from == (SecretEnvSource("app/config", "APP_", ("DB_HOST",)),)
    assert with_secret_env(base, {}, ()) is base


@pytest.mark.parametrize(
    ("fields", "code", "path"),
    [
        ({"secret_env": {"HOME": "app/home"}}, "invalid_value", "secret_env.HOME"),
        ({"secret_env": {"db-url": "app/db-url"}}, "invalid_value", "secret_env.db-url"),
        ({"secret_env": {"KEY_NAME": "AKIAEXAMPLE"}}, "secret_value", "secret_env.KEY_NAME"),
        ({"secret_env": {"DB": 3}}, "invalid_type", "secret_env.DB"),
        (
            {"env": {"DB": "x"}, "secret_env": {"DB": "app/db"}},
            "conflict",
            "secret_env.DB",
        ),
        (
            {"secret_env_from": [SecretEnvSource("app/config", prefix="LD_")]},
            "invalid_value",
            "secret_env_from[0].prefix",
        ),
        (
            {"secret_env_from": [SecretEnvSource("app/config", keys=("PATH",))]},
            "invalid_value",
            "secret_env_from[0].keys",
        ),
        ({"secret_env_from": ["app/config"]}, "invalid_type", "secret_env_from[0]"),
    ],
)
def test_a_project_built_in_code_validates_its_secret_env(
    fields: dict[str, Any], code: str, path: str
) -> None:
    with pytest.raises(AgentProjectException) as raised:
        project(**fields)
    assert (raised.value.code, raised.value.path) == (code, path)


def test_more_secrets_than_the_limit_are_refused() -> None:
    many = {f"VAR_{index}": f"app/secret-{index}" for index in range(AGENT_SECRET_ENV_MAX_SECRETS)}
    project(secret_env=many)
    with pytest.raises(AgentProjectException) as raised:
        project(secret_env={**many, "ONE_MORE": "app/one-more"})
    assert (raised.value.code, raised.value.path) == ("invalid_value", "secret_env")


def test_the_value_objects_check_their_types() -> None:
    with pytest.raises(InvalidArgumentException):
        SecretEnvRef("")
    with pytest.raises(InvalidArgumentException):
        SecretEnvRef("app/db", key="")
    with pytest.raises(InvalidArgumentException):
        SecretEnvSource("app/config", keys="DB_HOST")  # type: ignore[arg-type]
    assert SecretEnvSource("app/config", keys=["DB_HOST"]).keys == ("DB_HOST",)  # type: ignore[arg-type]


# --------------------------------------------------------------- reserved.json


def test_the_reserved_names_are_the_limits() -> None:
    assert list(AGENT_SECRET_ENV_RESERVED_NAMES) == RESERVED["names"]
    assert list(AGENT_SECRET_ENV_RESERVED_PREFIXES) == RESERVED["prefixes"]
    assert sorted(host.RESERVED_NAMES) == RESERVED["names"]
    assert list(host.RESERVED_PREFIXES) == RESERVED["prefixes"]


@pytest.mark.parametrize("case", RESERVED["cases"], ids=[c["name"] for c in RESERVED["cases"]])
def test_the_reserved_vectors(case: dict[str, Any]) -> None:
    assert is_reserved_name(case["name"]) is case["reserved"]
    assert host.is_reserved(case["name"]) is case["reserved"]


def test_the_host_constants_are_the_limits() -> None:
    assert host.SPEC_VARIABLE == AGENT_SECRET_ENV_VARIABLE
    assert host.SPEC_VERSION == AGENT_SECRET_ENV_SPEC_VERSION
    assert host.MAX_SECRETS == AGENT_SECRET_ENV_MAX_SECRETS
    assert host.MAX_TOTAL_BYTES == AGENT_SECRET_ENV_MAX_TOTAL_BYTES
    assert host.HELPER_TIMEOUT_SECONDS == AGENT_SECRET_ENV_HELPER_TIMEOUT_SECONDS


# ----------------------------------------------------------------- expand.json


@pytest.mark.parametrize("case", EXPAND["refCases"], ids=[c["name"] for c in EXPAND["refCases"]])
def test_the_reference_vectors(case: dict[str, Any]) -> None:
    if "error" in case:
        with pytest.raises(host.SecretEnvError) as raised:
            host.expand_reference(case["ref"], case["secretString"])
        assert raised.value.detail_code == case["error"]
        assert case["secretString"] not in str(raised.value)
        return
    assert host.expand_reference(case["ref"], case["secretString"]) == case["expected"]


@pytest.mark.parametrize(
    "case", EXPAND["sourceCases"], ids=[c["name"] for c in EXPAND["sourceCases"]]
)
def test_the_source_vectors(case: dict[str, Any]) -> None:
    if "error" in case:
        with pytest.raises(host.SecretEnvError) as raised:
            host.expand_source(case["source"], case["secretString"])
        assert raised.value.detail_code == case["error"]
        return
    assert host.expand_source(case["source"], case["secretString"]) == case["expected"]


@pytest.mark.parametrize(
    "case", EXPAND["sourceCases"], ids=[c["name"] for c in EXPAND["sourceCases"]]
)
def test_the_sdk_resolves_the_source_vectors_like_the_host(case: dict[str, Any]) -> None:
    spec = {"env": {}, "from": [case["source"]], "v": AGENT_SECRET_ENV_SPEC_VERSION}
    reads: list[tuple[str, str | None]] = []

    def read(secret: str, stage: str | None) -> str:
        reads.append((secret, stage))
        return str(case["secretString"])

    if "error" in case:
        with pytest.raises(AgentException) as raised:
            resolve_secret_env(spec, read)
        assert (raised.value.reason, raised.value.detail_code) == ("runtime_error", case["error"])
        assert case["source"]["secret"] not in str(raised.value)
        return
    assert resolve_secret_env(spec, read) == case["expected"]
    assert reads == [(case["source"]["secret"], None)]


def test_each_secret_is_read_once_and_a_repeated_name_is_refused() -> None:
    spec = {
        "env": {"A_URL": {"secret": "app/one"}, "B_URL": {"secret": "app/one"}},
        "from": [{"secret": "app/two", "keys": ["A_URL"]}],
        "v": AGENT_SECRET_ENV_SPEC_VERSION,
    }
    reads: list[str] = []

    def read(secret: str, stage: str | None) -> str:
        reads.append(secret)
        return '{"A_URL": "x"}' if secret == "app/two" else "value"

    with pytest.raises(AgentException) as raised:
        resolve_secret_env(spec, read)
    assert raised.value.detail_code == host.INVALID_KEY
    assert reads == ["app/one", "app/two"]


def test_the_total_size_of_the_values_is_capped() -> None:
    spec = {"env": {"BIG": {"secret": "app/big"}}, "from": [], "v": AGENT_SECRET_ENV_SPEC_VERSION}
    with pytest.raises(AgentException) as raised:
        resolve_secret_env(spec, lambda _secret, _stage: "x" * TOO_MANY_BYTES)
    assert raised.value.detail_code == host.INVALID


# ------------------------------------------------------------------- spec.json


@pytest.mark.parametrize("case", SPEC["cases"], ids=[c["name"] for c in SPEC["cases"]])
def test_the_spec_vectors(case: dict[str, Any]) -> None:
    secret_env, sources = _project_values(case["project"])
    assert spec_variable(secret_env, sources) == case["expected"]["variable"]
    readable = [*case["project"]["readable_secrets"], *secret_ids(secret_env, sources)]
    assert list(dict.fromkeys(readable)) == [
        _name_of(arn) for arn in case["expected"]["readableSecretArns"]
    ]


def _name_of(arn: str) -> str:
    """El nombre o ARN de partida de un ARN esperado del vector."""
    prefix = f"arn:aws:secretsmanager:{SPEC['region']}:{SPEC['account']}:secret:"
    name = arn.removeprefix(prefix)
    return name.removesuffix("-??????") if name.endswith("-??????") else arn


def test_the_spec_is_the_hosts_document() -> None:
    secret_env, sources = _project_values(SPEC["cases"][0]["project"])
    document = spec_document(secret_env, sources)
    assert document is not None
    assert host.parse_spec(json.dumps(document)) == document
    assert secret_env_names(secret_env, sources) == (
        "APP_DB_HOST",
        "APP_DB_USER",
        "DATABASE_URL",
        "OPENAI_API_KEY",
    )


# ------------------------------------------------------------------ dotenv.json


@pytest.mark.parametrize("case", DOTENV["cases"], ids=[c["name"] for c in DOTENV["cases"]])
def test_the_dotenv_vectors(case: dict[str, Any]) -> None:
    assert parse_dotenv(case["text"]) == case["expected"]
    assert list(dotenv_names(case["text"])) == case["names"]


# --------------------------------------------------------------- env-names.json


class _Tree:
    def __init__(self, files: dict[str, str]) -> None:
        self._files = files

    def list_files(self, max_depth: int, max_files: int) -> list[str]:
        return sorted(self._files)[:max_files]

    def read_text(self, path: str, max_bytes: int) -> str | None:
        return self._files.get(path)


@pytest.mark.parametrize("case", ENV_NAMES["cases"], ids=[c["name"] for c in ENV_NAMES["cases"]])
def test_the_env_names_vectors(case: dict[str, Any]) -> None:
    required = required_env_names(_Tree(case["files"]))
    assert list(required) == case["required"]
    missing = missing_env_names(required, set(case["covered"]), case["openPrefixes"])
    assert list(missing) == case["missing"]
    warnings = env_missing_warnings(missing)
    assert len(warnings) == len(missing)
    assert all(name in warning for name, warning in zip(missing, warnings, strict=True))


@pytest.mark.parametrize("field", ["key", "version_stage"])
def test_a_key_or_stage_outside_printable_ascii_is_refused(field: str) -> None:
    reference = SecretEnvRef("app/db", **{field: "clé con espacio"})
    with pytest.raises(AgentProjectException) as raised:
        project(secret_env={"DATABASE_URL": reference})
    assert (raised.value.code, raised.value.path) == ("invalid_value", "secret_env.DATABASE_URL")
