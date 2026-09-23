"""Aceptación del módulo git de `m9-e2b-v2-surface` contra AWS real
(`openspec/changes/m9-e2b-v2-surface/design.md` D23, tarea 11.3).

La secuencia de D23 sobre la fixture `sandbox` (900 s, se termina en
teardown): clone público superficial, operaciones locales, un remoto bare
dentro del propio sandbox (push y pull sin red), el error de upstream, los
errores de autenticación (sin credenciales y con un token falso) y la
visibilidad documentada de `dangerously_authenticate`. El repositorio es
`https://github.com/octocat/Hello-World.git` salvo `RAYITO_E2E_GIT_REPO`.

Higiene: el token falso y el secreto de `dangerously_authenticate` se
generan en cada corrida, nunca se imprimen y no pueden aparecer ni en los
mensajes de las excepciones ni en los registros del SDK (`caplog` a DEBUG),
ni quedar en la URL del remoto. Imprime `git_version` y los segundos de
cada operación.
"""

from __future__ import annotations

import logging
import os
import secrets
import time
from collections.abc import Callable
from typing import TypeVar

import pytest

from rayito import Sandbox
from rayito.exceptions import GitAuthException, GitUpstreamException

pytestmark = pytest.mark.e2e

T = TypeVar("T")

REPO_VAR = "RAYITO_E2E_GIT_REPO"
DEFAULT_REPO = "https://github.com/octocat/Hello-World.git"
HOME = "/home/user"
HELLO = f"{HOME}/hello"
REMOTE = f"{HOME}/remote.git"
WORK = f"{HOME}/work"
COPY = f"{HOME}/copy"
NETWORK_TIMEOUT_SECONDS = 120
CREDENTIAL_HOST = "example.invalid"


def repo_url() -> str:
    return os.environ.get(REPO_VAR) or DEFAULT_REPO


def assert_absent(secret: str, text: str, where: str) -> None:
    """Falla sin repetir ni el secreto ni el texto (el fallo no puede
    copiarlos al log de CI)."""
    leaked = secret in text
    assert not leaked, f"el secreto aparece en {where}"


class Timings:
    """Segundos por operación, impresos al final con su etiqueta."""

    def __init__(self) -> None:
        self.seconds: dict[str, float] = {}

    def run(self, label: str, action: Callable[[], T]) -> T:
        started = time.perf_counter()
        try:
            return action()
        finally:
            self.seconds[label] = time.perf_counter() - started

    def report(self) -> None:
        for label, seconds in self.seconds.items():
            print(f"[m9-git] {label}: {seconds:.2f} s", flush=True)


def test_git_sequence(sandbox: Sandbox, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG, logger="rayito")
    fake_token = f"rayito-e2e-fake-{secrets.token_hex(16)}"
    stored_secret = f"rayito-e2e-store-{secrets.token_hex(16)}"
    git = sandbox.git
    timings = Timings()

    git_version = sandbox.commands.run("git --version").stdout.strip()
    print(f"\n[m9-git] git_version={git_version}", flush=True)
    try:
        timings.run(
            "clone (depth=1)",
            lambda: git.clone(repo_url(), HELLO, depth=1, timeout=NETWORK_TIMEOUT_SECONDS),
        )
        default_branch = local_operations(sandbox, timings)
        bare_remote_round_trip(sandbox, timings)
        upstream_error(sandbox, timings)
        auth_errors(sandbox, timings, default_branch, fake_token)
        credential_store_visibility(sandbox, timings, stored_secret)
    finally:
        timings.report()
    assert_absent(fake_token, caplog.text, "los registros del SDK")
    assert_absent(stored_secret, caplog.text, "los registros del SDK")


def local_operations(sandbox: Sandbox, timings: Timings) -> str:
    git = sandbox.git
    timings.run("configure_user", lambda: git.configure_user("Rayito E2E", "e2e@example.invalid"))
    assert timings.run("status (limpio)", lambda: git.status(HELLO)).is_clean

    branches = timings.run("branches", lambda: git.branches(HELLO))
    default_branch = branches.current_branch
    assert default_branch is not None and default_branch in branches.branches, branches
    timings.run("create_branch", lambda: git.create_branch(HELLO, "feature"))
    timings.run("checkout_branch", lambda: git.checkout_branch(HELLO, default_branch))
    git.checkout_branch(HELLO, "feature")
    after = git.branches(HELLO)
    assert after.current_branch == "feature" and "feature" in after.branches, after

    sandbox.files.write(f"{HELLO}/e2e.txt", "hola\n")
    assert git.status(HELLO).has_untracked
    timings.run("add", lambda: git.add(HELLO))
    assert git.status(HELLO).has_staged
    timings.run("commit", lambda: git.commit(HELLO, "e2e: añade e2e.txt"))
    assert git.status(HELLO).is_clean

    timings.run("reset soft HEAD~1", lambda: git.reset(HELLO, mode="soft", target="HEAD~1"))
    assert git.status(HELLO).staged_count == 1
    timings.run("restore --staged", lambda: git.restore(HELLO, paths=["e2e.txt"], staged=True))
    restored = git.status(HELLO)
    assert not restored.has_staged and restored.has_untracked, restored

    timings.run(
        "set_config local",
        lambda: git.set_config("core.editor", "true", scope="local", path=HELLO),
    )
    value = timings.run(
        "get_config local", lambda: git.get_config("core.editor", scope="local", path=HELLO)
    )
    assert value == "true", value
    return default_branch


def bare_remote_round_trip(sandbox: Sandbox, timings: Timings) -> None:
    git = sandbox.git
    timings.run("init --bare", lambda: git.init(REMOTE, bare=True, initial_branch="main"))
    git.init(WORK, initial_branch="main")
    sandbox.files.write(f"{WORK}/one.txt", "uno\n")
    git.add(WORK)
    git.commit(WORK, "e2e: primero")
    timings.run("remote_add", lambda: git.remote_add(WORK, "origin", REMOTE))
    timings.run("push (bare)", lambda: git.push(WORK, remote="origin", branch="main"))

    timings.run("clone (bare)", lambda: git.clone(REMOTE, COPY))
    sandbox.files.write(f"{WORK}/two.txt", "dos\n")
    git.add(WORK)
    git.commit(WORK, "e2e: segundo")
    git.push(WORK)

    timings.run("pull (bare)", lambda: git.pull(COPY))
    log = sandbox.commands.run(f"git -C {COPY} log --format=%s").stdout
    assert log.splitlines()[:2] == ["e2e: segundo", "e2e: primero"], log


def upstream_error(sandbox: Sandbox, timings: Timings) -> None:
    git = sandbox.git
    git.create_branch(COPY, "no-upstream")
    with pytest.raises(GitUpstreamException):
        timings.run("pull sin upstream", lambda: git.pull(COPY))


def auth_errors(sandbox: Sandbox, timings: Timings, branch: str, fake_token: str) -> None:
    git = sandbox.git
    with pytest.raises(GitAuthException) as missing:
        timings.run(
            "push sin credenciales",
            lambda: git.push(
                HELLO, remote="origin", branch=branch, timeout=NETWORK_TIMEOUT_SECONDS
            ),
        )
    print(f"[m9-git] push sin credenciales: {missing.value}", flush=True)

    with pytest.raises(GitAuthException) as rejected:
        timings.run(
            "push con token falso",
            lambda: git.push(
                HELLO,
                remote="origin",
                branch=branch,
                username="rayito-e2e",
                password=fake_token,
                timeout=NETWORK_TIMEOUT_SECONDS,
            ),
        )
    assert_absent(fake_token, f"{rejected.value!s} {rejected.value!r}", "la excepción")
    remote = git.remote_get(HELLO, "origin")
    assert remote is not None
    assert_absent(fake_token, remote, "la URL del remoto")
    config = sandbox.commands.run(f"git -C {HELLO} config --list").stdout
    assert_absent(fake_token, config, "la configuración del repo")


def credential_store_visibility(sandbox: Sandbox, timings: Timings, secret: str) -> None:
    """Contrato documentado de E2B: las credenciales guardadas quedan en
    `~/.git-credentials`, legibles por el código del sandbox."""
    timings.run(
        "dangerously_authenticate",
        lambda: sandbox.git.dangerously_authenticate("e2e", secret, host=CREDENTIAL_HOST),
    )
    stored = sandbox.commands.run("cat ~/.git-credentials").stdout
    visible = any(CREDENTIAL_HOST in line and secret in line for line in stored.splitlines())
    assert visible, "~/.git-credentials no lleva la línea del host"
