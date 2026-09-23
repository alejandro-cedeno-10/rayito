"""Núcleo puro del módulo git (`sandbox.git`), compartido por `Git` y
`AsyncGit`: construcción de la línea de comandos, parsers de la salida
porcelain, credenciales en URL, redacción y clasificación de fallos. Sin I/O.

Los constructores de argumentos, los parsers y las listas de fragmentos de
error están adaptados del SDK de E2B (github.com/e2b-dev/E2B, Apache License
2.0) para que cada llamada produzca el mismo `git ...` que E2B; ver NOTICE.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Final, Literal
from urllib.parse import quote, urlparse, urlunparse

from rayito.exceptions import (
    CommandExitException,
    GitAuthException,
    GitUpstreamException,
    InvalidArgumentException,
)

GitResetMode = Literal["soft", "mixed", "hard", "merge", "keep"]

GIT_ENV: Final[Mapping[str, str]] = MappingProxyType({"GIT_TERMINAL_PROMPT": "0"})
RESET_MODES: Final[tuple[str, ...]] = ("soft", "mixed", "hard", "merge", "keep")
CONFIG_SCOPE_FLAGS: Final[Mapping[str, str]] = MappingProxyType(
    {"global": "--global", "local": "--local", "system": "--system"}
)
DEFAULT_CREDENTIAL_HOST: Final = "github.com"
DEFAULT_CREDENTIAL_PROTOCOL: Final = "https"
CREDENTIAL_URL_SCHEMES: Final = ("http", "https")
REDACTED: Final = "***"

AUTH_FAILURE_SNIPPETS: Final[tuple[str, ...]] = (
    "authentication failed",
    "terminal prompts disabled",
    "could not read username",
    "invalid username or password",
    "access denied",
    "permission denied",
    "not authorized",
)
MISSING_UPSTREAM_SNIPPETS: Final[tuple[str, ...]] = (
    "has no upstream branch",
    "no upstream branch",
    "no upstream configured",
    "no tracking information for the current branch",
    "no tracking information",
    "set the remote as upstream",
    "set the upstream branch",
    "please specify which branch you want to merge with",
)
CONFLICT_PAIRS: Final = frozenset({"DD", "AU", "UD", "UA", "DU", "AA", "UU"})
STATUS_BY_CODE: Final[tuple[tuple[str, str], ...]] = (
    ("U", "conflict"),
    ("R", "renamed"),
    ("C", "copied"),
    ("D", "deleted"),
    ("A", "added"),
    ("M", "modified"),
    ("T", "typechange"),
    ("?", "untracked"),
)


@dataclass(frozen=True)
class GitFileStatus:
    """Una entrada de `git status --porcelain=1`: `status` normalizado
    (`modified`, `added`, `untracked`, `conflict`...) y los dos caracteres
    crudos del índice y del árbol de trabajo."""

    name: str
    status: str
    index_status: str
    working_tree_status: str
    staged: bool
    renamed_from: str | None = None


@dataclass(frozen=True)
class GitStatus:
    """El estado de un repositorio tal como lo modela E2B."""

    current_branch: str | None
    upstream: str | None
    ahead: int
    behind: int
    detached: bool
    file_status: list[GitFileStatus] = field(default_factory=list)

    @property
    def is_clean(self) -> bool:
        return not self.file_status

    @property
    def has_changes(self) -> bool:
        return bool(self.file_status)

    @property
    def has_staged(self) -> bool:
        return any(entry.staged for entry in self.file_status)

    @property
    def has_untracked(self) -> bool:
        return any(entry.status == "untracked" for entry in self.file_status)

    @property
    def has_conflicts(self) -> bool:
        return any(entry.status == "conflict" for entry in self.file_status)

    @property
    def total_count(self) -> int:
        return len(self.file_status)

    @property
    def staged_count(self) -> int:
        return sum(1 for entry in self.file_status if entry.staged)

    @property
    def unstaged_count(self) -> int:
        return sum(1 for entry in self.file_status if not entry.staged)

    @property
    def untracked_count(self) -> int:
        return sum(1 for entry in self.file_status if entry.status == "untracked")

    @property
    def conflict_count(self) -> int:
        return sum(1 for entry in self.file_status if entry.status == "conflict")


@dataclass(frozen=True)
class GitBranches:
    branches: list[str] = field(default_factory=list)
    current_branch: str | None = None


@dataclass(frozen=True)
class ClonePlan:
    """Los argumentos de `git clone` y, si la URL llevaba credenciales que no
    deben quedarse, el `remote set-url origin` posterior."""

    args: tuple[str, ...]
    repo_path: str | None
    sanitized_url: str | None
    should_strip: bool


@dataclass(frozen=True)
class GitCommandOptions:
    """Lo que cada método pasa tal cual a `commands.run`: `timeout=None` es
    sin plazo de servidor, como en el módulo git de E2B."""

    envs: Mapping[str, str] | None = None
    user: str | None = None
    cwd: str | None = None
    timeout: float | None = None
    request_timeout: float | None = None

    def merged_envs(self) -> dict[str, str]:
        """`GIT_TERMINAL_PROMPT=0` debajo de los `envs` del caller (los suyos ganan)."""
        return {**GIT_ENV, **(self.envs or {})}


def shell_quote(value: str) -> str:
    return "'" + value.replace("'", "'\"'\"'") + "'"


def git_command(args: Sequence[str], repo_path: str | None = None) -> str:
    """`'git' ['-C' '<repo>'] '<arg>'...` con cada parte entre comillas simples."""
    location = ["-C", repo_path] if repo_path else []
    return " ".join(shell_quote(part) for part in ["git", *location, *args])


def status_args() -> list[str]:
    return ["status", "--porcelain=1", "-b"]


def branches_args() -> list[str]:
    return ["branch", "--format=%(refname:short)\t%(HEAD)"]


def create_branch_args(branch: str) -> list[str]:
    return ["checkout", "-b", branch]


def checkout_branch_args(branch: str) -> list[str]:
    return ["checkout", branch]


def delete_branch_args(branch: str, force: bool) -> list[str]:
    return ["branch", "-D" if force else "-d", branch]


def add_args(files: Sequence[str] | None, stage_all: bool) -> list[str]:
    if not files:
        return ["add", "-A" if stage_all else "."]
    return ["add", "--", *files]


def commit_args(
    message: str, author_name: str | None, author_email: str | None, allow_empty: bool
) -> list[str]:
    author = [
        *(["-c", f"user.name={author_name}"] if author_name else []),
        *(["-c", f"user.email={author_email}"] if author_email else []),
    ]
    return [*author, "commit", "-m", message, *(["--allow-empty"] if allow_empty else [])]


def reset_args(mode: str | None, target: str | None, paths: Sequence[str] | None) -> list[str]:
    if mode and mode not in RESET_MODES:
        raise InvalidArgumentException(
            f"reset: el modo debe ser uno de {', '.join(RESET_MODES)}, recibido {mode!r}"
        )
    return [
        "reset",
        *([f"--{mode}"] if mode else []),
        *([target] if target else []),
        *(["--", *paths] if paths else []),
    ]


def resolve_restore_targets(staged: bool | None, worktree: bool | None) -> tuple[bool, bool]:
    """La regla de E2B: sin nada, sólo el árbol de trabajo; `staged=True` solo
    no toca el árbol; `worktree` dado solo no toca el índice."""
    if staged is None and worktree is None:
        return False, True
    if staged is True and worktree is None:
        return True, False
    if staged is None:
        return False, bool(worktree)
    return bool(staged), bool(worktree)


def restore_args(
    paths: Sequence[str], staged: bool | None, worktree: bool | None, source: str | None
) -> list[str]:
    if not paths:
        raise InvalidArgumentException("restore: hace falta al menos una ruta")
    restore_staged, restore_worktree = resolve_restore_targets(staged, worktree)
    if not restore_staged and not restore_worktree:
        raise InvalidArgumentException("restore: staged o worktree tiene que ser True")
    return [
        "restore",
        *(["--worktree"] if restore_worktree else []),
        *(["--staged"] if restore_staged else []),
        *(["--source", source] if source else []),
        "--",
        *paths,
    ]


def init_args(path: str, bare: bool, initial_branch: str | None) -> list[str]:
    return [
        "init",
        *(["--initial-branch", initial_branch] if initial_branch else []),
        *(["--bare"] if bare else []),
        path,
    ]


def remote_add_args(name: str, url: str, fetch: bool) -> list[str]:
    if not name or not url:
        raise InvalidArgumentException("remote_add: hacen falta el nombre y la URL del remoto")
    return ["remote", "add", *(["-f"] if fetch else []), name, url]


def remote_list_args() -> list[str]:
    return ["remote"]


def remote_get_url_args(name: str) -> list[str]:
    return ["remote", "get-url", name]


def remote_set_url_args(name: str, url: str) -> list[str]:
    return ["remote", "set-url", name, url]


def remote_add_overwrite_command(path: str, name: str, url: str, fetch: bool) -> str:
    """`remote add` que, si el remoto existe, cae a `remote set-url`; con
    `fetch`, un `git fetch <name>` detrás (la forma de E2B)."""
    add = git_command(remote_add_args(name, url, fetch), path)
    set_url = git_command(remote_set_url_args(name, url), path)
    command = f"{add} || {set_url}"
    if not fetch:
        return command
    return f"({command}) && {git_command(['fetch', name], path)}"


def remote_get_command(path: str, name: str) -> str:
    if not name:
        raise InvalidArgumentException("remote_get: hace falta el nombre del remoto")
    return f"{git_command(remote_get_url_args(name), path)} || true"


def has_upstream_args() -> list[str]:
    return ["rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"]


def push_args(
    remote_name: str | None, *, remote: str | None, branch: str | None, set_upstream: bool
) -> list[str]:
    target = remote_name or remote
    return [
        "push",
        *(["--set-upstream"] if set_upstream and target else []),
        *([target] if target else []),
        *([branch] if branch else []),
    ]


def pull_args(remote: str | None, branch: str | None, remote_name: str | None = None) -> list[str]:
    target = remote_name or remote
    return ["pull", *([target] if target else []), *([branch] if branch else [])]


def resolve_config_scope(scope: str | None, path: str | None) -> tuple[str, str | None]:
    """`(flag, ruta del repo)`: `--local` exige `path`; `--global` y
    `--system` no usan `-C`."""
    name = (scope or "global").strip().lower()
    flag = CONFIG_SCOPE_FLAGS.get(name)
    if flag is None:
        raise InvalidArgumentException("config: scope debe ser global, local o system")
    if name != "local":
        return flag, None
    if not path:
        raise InvalidArgumentException("config: scope='local' necesita path (el repositorio)")
    return flag, path


def require_config_key(key: str) -> str:
    if not key:
        raise InvalidArgumentException("config: hace falta la clave")
    return key


def config_set_args(scope_flag: str, key: str, value: str) -> list[str]:
    return ["config", scope_flag, key, value]


def config_get_command(scope_flag: str, key: str, repo_path: str | None) -> str:
    return f"{git_command(['config', scope_flag, '--get', key], repo_path)} || true"


def credential_approve_command(username: str, password: str, host: str, protocol: str) -> str:
    """`printf %s '<protocol/host/username/password>' | git credential
    approve`: el helper `store` lo guarda en `~/.git-credentials`."""
    lines = [
        f"protocol={protocol.strip() or DEFAULT_CREDENTIAL_PROTOCOL}",
        f"host={host.strip() or DEFAULT_CREDENTIAL_HOST}",
        f"username={username}",
        f"password={password}",
        "",
        "",
    ]
    return (
        f"printf %s {shell_quote(chr(10).join(lines))} | {git_command(['credential', 'approve'])}"
    )


def require_both_credentials(username: str | None, password: str | None) -> None:
    if not username or not password:
        raise InvalidArgumentException("dangerously_authenticate: hacen falta username y password")


def require_username_for_password(username: str | None, password: str | None, action: str) -> None:
    if password and not username:
        raise InvalidArgumentException(f"git {action}: un password/token necesita también username")


def require_user_identity(name: str, email: str) -> None:
    if not name or not email:
        raise InvalidArgumentException("configure_user: hacen falta name y email")


def with_credentials(url: str, username: str | None, password: str | None) -> str:
    """La URL http(s) con `usuario:password@` codificados con `%XX` (así un
    `@` o un `:` del token no rompen la URL)."""
    if not username and not password:
        return url
    if not username or not password:
        raise InvalidArgumentException("git: las credenciales necesitan username y password")
    parsed = urlparse(url)
    if parsed.scheme not in CREDENTIAL_URL_SCHEMES:
        raise InvalidArgumentException(
            "git: sólo las URL http(s) admiten credenciales usuario/password"
        )
    userinfo = f"{quote(username, safe='')}:{quote(password, safe='')}"
    return urlunparse(parsed._replace(netloc=f"{userinfo}@{parsed.netloc}"))


def strip_credentials(url: str) -> str:
    parsed = urlparse(url)
    if parsed.scheme not in CREDENTIAL_URL_SCHEMES:
        return url
    if not parsed.username and not parsed.password:
        return url
    host = parsed.hostname or ""
    netloc = f"{host}:{parsed.port}" if parsed.port else host
    return urlunparse(parsed._replace(netloc=netloc))


def derive_repo_dir_from_url(url: str) -> str | None:
    """El directorio que `git clone <url>` crearía; `None` si la URL no es
    http(s) o no tiene un último segmento."""
    parsed = urlparse(url)
    if parsed.scheme not in CREDENTIAL_URL_SCHEMES:
        return None
    last_segment = parsed.path.rstrip("/").rsplit("/", 1)[-1]
    if not last_segment:
        return None
    return last_segment.removesuffix(".git") or None


def build_clone_plan(
    url: str,
    path: str | None,
    branch: str | None,
    depth: int | None,
    username: str | None,
    password: str | None,
    dangerously_store_credentials: bool,
) -> ClonePlan:
    """Sin `path` derivable y con credenciales que hay que quitar después es
    `InvalidArgumentException` antes de ejecutar nada."""
    clone_url = with_credentials(url, username, password) if username and password else url
    sanitized_url = strip_credentials(clone_url)
    should_strip = not dangerously_store_credentials and sanitized_url != clone_url
    repo_path = (path or derive_repo_dir_from_url(url)) if should_strip else path
    if should_strip and not repo_path:
        raise InvalidArgumentException(
            "clone: con credenciales hace falta path (no se deduce de la URL) para quitarlas "
            "del remoto después"
        )
    args = (
        "clone",
        clone_url,
        *(("--branch", branch, "--single-branch") if branch else ()),
        *(("--depth", str(depth)) if depth else ()),
        *((path,) if path else ()),
    )
    return ClonePlan(
        args=args,
        repo_path=repo_path,
        sanitized_url=sanitized_url if should_strip else None,
        should_strip=should_strip,
    )


def parse_ahead_behind(segment: str | None) -> tuple[int, int]:
    """`"ahead 2, behind 1"` → `(2, 1)`; lo que no se entienda cuenta 0."""
    counts = {"ahead": 0, "behind": 0}
    for part in (segment or "").split(","):
        label, _, number = part.strip().partition(" ")
        if label in counts and number.strip().isdigit():
            counts[label] = int(number)
    return counts["ahead"], counts["behind"]


def normalize_branch_name(name: str) -> str:
    if name.startswith("HEAD (detached at "):
        return name.removeprefix("HEAD (detached at ").rstrip(")")
    return (
        name.replace("HEAD (no branch)", "HEAD")
        .replace("No commits yet on ", "")
        .replace("Initial commit on ", "")
    )


def derive_file_status(index_status: str, working_status: str) -> str:
    """Los siete pares de conflicto de porcelain v1 (`DD`, `AU`, `UD`, `UA`,
    `DU`, `AA`, `UU`) son `conflict`; el resto sigue la precedencia de E2B."""
    if index_status + working_status in CONFLICT_PAIRS:
        return "conflict"
    codes = {index_status, working_status}
    return next((status for code, status in STATUS_BY_CODE if code in codes), "unknown")


@dataclass(frozen=True)
class BranchLine:
    current_branch: str | None = None
    upstream: str | None = None
    ahead: int = 0
    behind: int = 0
    detached: bool = False


def parse_branch_line(line: str) -> BranchLine:
    """`## <rama>[...<upstream>] [ahead N, behind M]`, `No commits yet on`,
    `HEAD (no branch)` o `HEAD (detached at <sha>)`."""
    if not line.startswith("## "):
        return BranchLine()
    info = line[3:]
    bracket = info.find(" [")
    branch_part = info if bracket == -1 else info[:bracket]
    ahead, behind = parse_ahead_behind(None if bracket == -1 else info[bracket + 2 : -1])
    normalized = normalize_branch_name(branch_part)
    if "detached" in branch_part or normalized.startswith("HEAD"):
        return BranchLine(ahead=ahead, behind=behind, detached=True)
    branch, separator, upstream = normalized.partition("...")
    return BranchLine(
        current_branch=branch or None,
        upstream=(upstream or None) if separator else None,
        ahead=ahead,
        behind=behind,
    )


def parse_file_line(line: str) -> GitFileStatus | None:
    if line.startswith("?? "):
        return GitFileStatus(
            name=line[3:],
            status="untracked",
            index_status="?",
            working_tree_status="?",
            staged=False,
        )
    if len(line) < 3:
        return None
    index_status, working_status, path = line[0], line[1], line[3:]
    renamed_from, arrow, renamed_to = path.partition(" -> ")
    return GitFileStatus(
        name=renamed_to if arrow else path,
        status=derive_file_status(index_status, working_status),
        index_status=index_status,
        working_tree_status=working_status,
        staged=index_status not in (" ", "?"),
        renamed_from=renamed_from if arrow else None,
    )


def parse_git_status(output: str) -> GitStatus:
    """`git status --porcelain=1 -b` → `GitStatus`."""
    lines = [line.rstrip() for line in output.split("\n") if line.strip()]
    if not lines:
        return GitStatus(current_branch=None, upstream=None, ahead=0, behind=0, detached=False)
    branch = parse_branch_line(lines[0])
    entries = [entry for entry in map(parse_file_line, lines[1:]) if entry is not None]
    return GitStatus(
        current_branch=branch.current_branch,
        upstream=branch.upstream,
        ahead=branch.ahead,
        behind=branch.behind,
        detached=branch.detached,
        file_status=entries,
    )


def parse_git_branches(output: str) -> GitBranches:
    """`git branch --format=%(refname:short)\\t%(HEAD)` → `GitBranches`."""
    branches: list[str] = []
    current: str | None = None
    for line in (line.strip() for line in output.split("\n")):
        if not line:
            continue
        name, _, head = line.partition("\t")
        branches.append(name)
        if head == "*":
            current = name
    return GitBranches(branches=branches, current_branch=current)


def parse_remote_url(output: str, remote: str) -> str:
    url = output.strip()
    if not url:
        raise InvalidArgumentException(f"el remoto {remote!r} no tiene URL en el repositorio")
    return url


def resolve_remote_name(remote: str | None, remotes_output: str) -> str:
    """El remoto al que inyectar credenciales: el dado; si no, el único que
    haya; con varios, `origin` si existe."""
    if remote:
        return remote
    remotes = [line.strip() for line in remotes_output.splitlines() if line.strip()]
    if len(remotes) == 1:
        return remotes[0]
    if "origin" in remotes:
        return "origin"
    raise InvalidArgumentException(
        "git: con username/password y varios remotos (ninguno origin) hace falta remote="
    )


def credential_secrets(password: str | None) -> tuple[str, ...]:
    """Las formas de un password que pueden aparecer en la salida de git: la
    cruda y la codificada con `%XX` de la URL."""
    if not password:
        return ()
    encoded = quote(password, safe="")
    return (password,) if encoded == password else (password, encoded)


def redact(text: str, secrets: Sequence[str]) -> str:
    for secret in sorted((secret for secret in secrets if secret), key=len, reverse=True):
        text = text.replace(secret, REDACTED)
    return text


def failure_output(exc: CommandExitException) -> str:
    return f"{exc.stderr}\n{exc.stdout}".lower()


def is_auth_failure(exc: Exception) -> bool:
    if not isinstance(exc, CommandExitException):
        return False
    output = failure_output(exc)
    return any(snippet in output for snippet in AUTH_FAILURE_SNIPPETS)


def is_missing_upstream(exc: Exception) -> bool:
    if not isinstance(exc, CommandExitException):
        return False
    output = failure_output(exc)
    return any(snippet in output for snippet in MISSING_UPSTREAM_SNIPPETS)


def auth_error_message(action: str, missing_password: bool) -> str:
    if missing_password:
        return f"git {action} necesita un password/token para repositorios privados"
    return f"git {action} necesita credenciales para repositorios privados"


def upstream_error_message(action: str) -> str:
    if action == "push":
        return (
            "git push falló porque la rama no tiene upstream: fíjalo una vez con "
            "set_upstream=True (con remote/branch si hace falta) o pasa remote y branch"
        )
    return (
        "git pull falló porque la rama no tiene upstream: pasa remote y branch, o fíjalo una "
        "vez (push con set_upstream=True o git branch --set-upstream-to=origin/<rama> <rama>)"
    )


def redacted_exit(exc: CommandExitException, secrets: Sequence[str]) -> CommandExitException:
    """Una copia del fallo sin ningún secreto en el mensaje, la salida ni el
    campo `error`."""
    return CommandExitException(
        redact(str(exc), secrets),
        exit_code=exc.exit_code,
        stdout=redact(exc.stdout, secrets),
        stderr=redact(exc.stderr, secrets),
        error=None if exc.error is None else redact(exc.error, secrets),
        grpc_code=exc.grpc_code,
    )


@dataclass(frozen=True)
class FailurePolicy:
    """Cómo traducir el `CommandExitException` de un comando git: `action`
    nombra la operación en el mensaje, `remote` activa la clasificación de
    autenticación (sólo las operaciones que hablan con un remoto), `upstream`
    la de rama sin upstream (push/pull) y `secrets` lo que hay que redactar."""

    action: str
    remote: bool = False
    upstream: bool = False
    missing_password: bool = False
    secrets: tuple[str, ...] = ()


def git_failure(exc: CommandExitException, policy: FailurePolicy) -> Exception:
    """La excepción a lanzar: `GitAuthException`, `GitUpstreamException`, el
    mismo `exc` si no hay nada que redactar, o su copia redactada."""
    if policy.remote and is_auth_failure(exc):
        return GitAuthException(auth_error_message(policy.action, policy.missing_password))
    if policy.upstream and is_missing_upstream(exc):
        return GitUpstreamException(upstream_error_message(policy.action))
    if not policy.secrets:
        return exc
    return redacted_exit(exc, policy.secrets)


__all__ = [
    "GIT_ENV",
    "ClonePlan",
    "FailurePolicy",
    "GitBranches",
    "GitCommandOptions",
    "GitFileStatus",
    "GitResetMode",
    "GitStatus",
    "build_clone_plan",
    "git_command",
    "git_failure",
    "parse_git_branches",
    "parse_git_status",
    "redact",
    "shell_quote",
    "strip_credentials",
    "with_credentials",
]
