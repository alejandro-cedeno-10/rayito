"""`ContextHasher` (m15-templates): qué ficheros locales entran en el
contexto de build, filtrados por `.dockerignore`, y un hash determinista
sobre su contenido."""

from __future__ import annotations

import warnings
from pathlib import Path

import pytest

from rayito._templates._context import (
    DockerIgnore,
    collect_context_files,
    files_hash,
    sensitive_paths,
)
from rayito._templates._instructions import CopyStep
from rayito.exceptions import BuildException

PROC_SELF_ENVIRON = "/proc/self/environ"


def test_dockerignore_matches_simple_globs_and_negation() -> None:
    ignore = DockerIgnore.from_text("*.pyc\n!keep.pyc\n")
    assert ignore.matches("a.pyc") is True
    assert ignore.matches("keep.pyc") is False
    assert ignore.matches("a.py") is False


def test_collect_context_files_reads_a_single_file_and_a_directory(tmp_path: Path) -> None:
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "main.py").write_text("print(1)")
    (tmp_path / "app" / "__pycache__").mkdir()
    (tmp_path / "app" / "__pycache__" / "main.pyc").write_bytes(b"\x00")
    (tmp_path / "requirements.txt").write_text("pandas\n")
    (tmp_path / ".dockerignore").write_text("**/__pycache__/*\n__pycache__/*\n")

    entries = collect_context_files(
        tmp_path,
        [CopyStep("app/", "/srv/app/"), CopyStep("requirements.txt", "/srv/requirements.txt")],
    )
    paths = [relpath for relpath, _ in entries]
    assert "app/main.py" in paths
    assert "requirements.txt" in paths
    assert not any("pyc" in path for path in paths)
    assert paths == sorted(paths)


def test_collect_context_files_raises_build_exception_for_a_missing_source(tmp_path: Path) -> None:
    with pytest.raises(BuildException, match="no existe"):
        collect_context_files(tmp_path, [CopyStep("missing/", "/srv/missing/")])


def test_collect_context_files_works_with_the_default_context_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Reproduce the blocker: `Template.build(t, name, bucket=...)` with no
    `context_dir=` resolves `Path()` to the current directory before
    comparing it to an already-absolute `src`; without resolving the root
    first, `relative_to` raised a raw `ValueError`."""
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "main.py").write_text("print(1)")
    monkeypatch.chdir(tmp_path)

    entries = collect_context_files(Path(), [CopyStep("app/", "/srv/app/")])

    assert dict(entries)["app/main.py"] == b"print(1)"


def test_collect_context_files_rejects_a_source_outside_the_context(tmp_path: Path) -> None:
    context_dir = tmp_path / "context"
    context_dir.mkdir()
    (tmp_path / "secret.txt").write_text("outside")

    with pytest.raises(BuildException) as excinfo:
        collect_context_files(context_dir, [CopyStep("../secret.txt", "/srv/secret.txt")])
    assert excinfo.value.reason == "context_path_outside"


def test_files_hash_is_order_independent_and_content_sensitive() -> None:
    a = [("x.py", b"1"), ("y.py", b"2")]
    b = [("y.py", b"2"), ("x.py", b"1")]
    c = [("x.py", b"1"), ("y.py", b"3")]
    assert files_hash(a) == files_hash(b)
    assert files_hash(a) != files_hash(c)


def _context_with_escaping_links(tmp_path: Path) -> Path:
    """`ctx/app/` con un fichero normal, un enlace a un fichero de fuera del
    contexto y un enlace a un directorio de fuera."""
    outside = tmp_path / "outside"
    (outside / "dir").mkdir(parents=True)
    (outside / "secret").write_text("AWS_SECRET=shh\n")
    (outside / "dir" / "inner").write_text("INNER\n")
    app = tmp_path / "ctx" / "app"
    app.mkdir(parents=True)
    (app / "main.py").write_text("print(1)\n")
    (app / "config").symlink_to(outside / "secret")
    (app / "linked").symlink_to(outside / "dir", target_is_directory=True)
    return tmp_path / "ctx"


def test_collect_context_files_skips_symlinks_that_point_outside(tmp_path: Path) -> None:
    """Como Docker y el SDK de TypeScript: un enlace simbólico dentro de un
    directorio copiado nunca se sigue, así que un `config -> ~/.aws/...` no
    acaba en el artefacto ni en la imagen."""
    ctx = _context_with_escaping_links(tmp_path)

    entries = collect_context_files(ctx, [CopyStep("app", "/app")])

    assert entries == (("app/main.py", b"print(1)\n"),)


def test_collect_context_files_skips_symlinks_inside_the_context_too(tmp_path: Path) -> None:
    """Una sola regla para Python y TypeScript: los enlaces de dentro de un
    directorio copiado se omiten aunque apunten dentro del contexto."""
    app = tmp_path / "app"
    app.mkdir()
    (app / "real.txt").write_text("real")
    (app / "alias.txt").symlink_to(app / "real.txt")

    entries = collect_context_files(tmp_path, [CopyStep("app", "/app")])

    assert entries == (("app/real.txt", b"real"),)


@pytest.mark.skipif(
    not Path(PROC_SELF_ENVIRON).exists(), reason="sólo Linux tiene /proc/self/environ"
)
def test_collect_context_files_skips_a_deep_link_to_proc_self_environ(tmp_path: Path) -> None:
    """Un enlace a una ruta absoluta (el entorno del proceso que construye,
    con sus credenciales) a cualquier profundidad nunca se lee."""
    deep = tmp_path / "app" / "a" / "b"
    deep.mkdir(parents=True)
    (deep / "env").symlink_to(PROC_SELF_ENVIRON)
    (deep / "ok.txt").write_text("ok")

    entries = collect_context_files(tmp_path, [CopyStep("app", "/app")])

    assert entries == (("app/a/b/ok.txt", b"ok"),)


def test_a_top_level_src_that_links_outside_is_rejected(tmp_path: Path) -> None:
    (tmp_path / "secret").write_text("outside")
    ctx = tmp_path / "ctx"
    ctx.mkdir()
    (ctx / "config").symlink_to(tmp_path / "secret")

    with pytest.raises(BuildException) as excinfo:
        collect_context_files(ctx, [CopyStep("config", "/srv/config")])
    assert excinfo.value.reason == "context_path_outside"


def test_a_top_level_src_that_links_inside_the_context_is_read(tmp_path: Path) -> None:
    """Como el `COPY` de Docker: un `src` de primer nivel que es un enlace a
    un fichero del propio contexto se lee (con la ruta del destino)."""
    (tmp_path / "real.txt").write_text("real")
    (tmp_path / "alias.txt").symlink_to(tmp_path / "real.txt")

    entries = collect_context_files(tmp_path, [CopyStep("alias.txt", "/srv/a.txt")])

    assert entries == (("real.txt", b"real"),)


DOCKER_INIT_IGNORE = "**/.env\n**/.git\n**/secrets.dev.yaml\n**/node_modules\n"


def _context_with_root_secrets(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text("API_KEY=shh\n")
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "config").write_text("[remote]\n")
    (tmp_path / "secrets.dev.yaml").write_text("token: shh\n")
    (tmp_path / "node_modules" / "x").mkdir(parents=True)
    (tmp_path / "node_modules" / "x" / "index.js").write_text("")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / ".env").write_text("NESTED=shh\n")
    (tmp_path / "main.py").write_text("print(1)\n")


def test_docker_init_ignore_excludes_root_secrets_too(tmp_path: Path) -> None:
    """`**/.env` (lo que genera `docker init`) excluye también el `.env`,
    `.git/` y `secrets.dev.yaml` de la raíz, como Docker."""
    _context_with_root_secrets(tmp_path)
    (tmp_path / ".dockerignore").write_text(DOCKER_INIT_IGNORE)

    with warnings.catch_warnings():
        warnings.simplefilter("error")
        entries = collect_context_files(tmp_path, [CopyStep(".", "/app")])

    assert [path for path, _ in entries] == [".dockerignore", "main.py"]


def test_packaging_likely_secrets_warns_without_the_contents(tmp_path: Path) -> None:
    _context_with_root_secrets(tmp_path)

    with pytest.warns(UserWarning, match=r"\.dockerignore") as caught:
        entries = collect_context_files(tmp_path, [CopyStep(".", "/app")])

    message = str(caught[0].message)
    assert ".env" in message
    assert "shh" not in message
    assert ".env" in dict(entries)


def test_sensitive_paths_flags_env_git_keys_and_cloud_credentials() -> None:
    flagged = sensitive_paths(
        [".env", ".env.local", "a/.git/HEAD", ".aws/credentials", "k.pem", "src/app.py", ".envrc"]
    )
    assert flagged == (".env", ".env.local", "a/.git/HEAD", ".aws/credentials", "k.pem")
