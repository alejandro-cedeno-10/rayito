/**
 * Piezas puras del módulo git (la contraparte camelCase de `_git_base.py`):
 * comillas de shell, los argv exactos de E2B, los parsers de `git status
 * --porcelain=1 -b` y de `git branch`, las credenciales en la URL, la
 * redacción de secretos y la clasificación de fallos. Sin I/O: `Git` las
 * compone sobre `commands.run`. Adaptado del SDK de E2B (Apache-2.0).
 */

import { CommandExitError, InvalidArgumentError } from "../errors.js";

export const GIT_ENV: Readonly<Record<string, string>> = Object.freeze({
  GIT_TERMINAL_PROMPT: "0",
});

export const GIT_RESET_MODES = ["soft", "mixed", "hard", "merge", "keep"] as const;
export type GitResetMode = (typeof GIT_RESET_MODES)[number];

export const GIT_CONFIG_SCOPES = ["global", "local", "system"] as const;
export type GitConfigScope = (typeof GIT_CONFIG_SCOPES)[number];

export type GitStatusLabel =
  | "conflict"
  | "renamed"
  | "copied"
  | "deleted"
  | "added"
  | "modified"
  | "typechange"
  | "untracked"
  | "unknown";

export type GitAction = "clone" | "push" | "pull" | "remote add";

export const REDACTED = "***";

const AUTH_FAILURE_SNIPPETS: readonly string[] = [
  "authentication failed",
  "terminal prompts disabled",
  "could not read username",
  "invalid username or password",
  "access denied",
  "permission denied",
  "not authorized",
];

const MISSING_UPSTREAM_SNIPPETS: readonly string[] = [
  "has no upstream branch",
  "no upstream branch",
  "no upstream configured",
  "no tracking information for the current branch",
  "no tracking information",
  "set the remote as upstream",
  "set the upstream branch",
  "please specify which branch you want to merge with",
];

const CONFLICT_CODES: ReadonlySet<string> = new Set(["DD", "AU", "UD", "UA", "DU", "AA", "UU"]);
const HTTP_URL = /^(https?:\/\/)([^/?#]*)(.*)$/i;
const RFC3986_EXTRA = /[!'()*]/g;

export interface GitFileStatus {
  readonly name: string;
  readonly status: GitStatusLabel;
  readonly indexStatus: string;
  readonly workingTreeStatus: string;
  readonly staged: boolean;
  readonly renamedFrom?: string | undefined;
}

export interface GitStatus {
  readonly currentBranch: string | undefined;
  readonly upstream: string | undefined;
  readonly ahead: number;
  readonly behind: number;
  readonly detached: boolean;
  readonly fileStatus: readonly GitFileStatus[];
  readonly isClean: boolean;
  readonly hasChanges: boolean;
  readonly hasStaged: boolean;
  readonly hasUntracked: boolean;
  readonly hasConflicts: boolean;
  readonly totalCount: number;
  readonly stagedCount: number;
  readonly unstagedCount: number;
  readonly untrackedCount: number;
  readonly conflictCount: number;
}

export interface GitBranches {
  readonly branches: readonly string[];
  readonly currentBranch: string | undefined;
}

/** Lo que `clone` ejecuta: el argv, el repo donde quitar las credenciales y la URL limpia. */
export interface ClonePlan {
  readonly args: readonly string[];
  readonly repoPath: string | undefined;
  readonly sanitizedUrl: string | undefined;
}

export interface ClonePlanInput {
  readonly url: string;
  readonly path?: string | undefined;
  readonly branch?: string | undefined;
  readonly depth?: number | undefined;
  readonly username?: string | undefined;
  readonly password?: string | undefined;
  readonly dangerouslyStoreCredentials?: boolean | undefined;
}

/** Siempre entre comillas simples, también lo que no las necesita: el comando grabado es predecible. */
export function shellQuote(value: string): string {
  return `'${value.replaceAll("'", `'"'"'`)}'`;
}

export function gitCommand(args: readonly string[], repoPath?: string): string {
  const parts = ["git", ...(repoPath ? ["-C", repoPath] : []), ...args];
  return parts.map(shellQuote).join(" ");
}

/** `GIT_TERMINAL_PROMPT=0` debajo de las envs del caller: las suyas ganan. */
export function gitEnvs(
  envs: Readonly<Record<string, string>> | undefined,
): Readonly<Record<string, string>> {
  return { ...GIT_ENV, ...(envs ?? {}) };
}

export function statusArgs(): string[] {
  return ["status", "--porcelain=1", "-b"];
}

export function branchesArgs(): string[] {
  return ["branch", "--format=%(refname:short)\t%(HEAD)"];
}

export function createBranchArgs(branch: string): string[] {
  return ["checkout", "-b", branch];
}

export function checkoutBranchArgs(branch: string): string[] {
  return ["checkout", branch];
}

export function deleteBranchArgs(branch: string, force: boolean): string[] {
  return ["branch", force ? "-D" : "-d", branch];
}

export function addArgs(files: readonly string[] | undefined, all: boolean): string[] {
  if (files === undefined || files.length === 0) {
    return ["add", all ? "-A" : "."];
  }
  return ["add", "--", ...files];
}

export function commitArgs(
  message: string,
  options: {
    readonly authorName?: string | undefined;
    readonly authorEmail?: string | undefined;
    readonly allowEmpty?: boolean | undefined;
  } = {},
): string[] {
  const author = [
    ...(options.authorName ? ["-c", `user.name=${options.authorName}`] : []),
    ...(options.authorEmail ? ["-c", `user.email=${options.authorEmail}`] : []),
  ];
  return [...author, "commit", "-m", message, ...(options.allowEmpty ? ["--allow-empty"] : [])];
}

export function validateResetMode(mode: unknown): GitResetMode | undefined {
  if (mode === undefined) {
    return undefined;
  }
  if (typeof mode !== "string" || !(GIT_RESET_MODES as readonly string[]).includes(mode)) {
    throw new InvalidArgumentError(
      `el modo de reset debe ser uno de ${GIT_RESET_MODES.join(", ")}, recibido ${String(mode)}`,
    );
  }
  return mode as GitResetMode;
}

export function resetArgs(
  options: {
    readonly mode?: GitResetMode | undefined;
    readonly target?: string | undefined;
    readonly paths?: readonly string[] | undefined;
  } = {},
): string[] {
  const mode = validateResetMode(options.mode);
  return [
    "reset",
    ...(mode ? [`--${mode}`] : []),
    ...(options.target ? [options.target] : []),
    ...(options.paths && options.paths.length > 0 ? ["--", ...options.paths] : []),
  ];
}

/**
 * La regla de E2B: sin `staged` ni `worktree` restaura el árbol de trabajo;
 * `staged: true` sólo el índice; un único flag explícito deja el otro en
 * `false`. Los dos en `false` o ninguna ruta es `InvalidArgumentError`.
 */
export function restoreArgs(
  paths: readonly string[] | undefined,
  options: {
    readonly staged?: boolean | undefined;
    readonly worktree?: boolean | undefined;
    readonly source?: string | undefined;
  } = {},
): string[] {
  if (paths === undefined || paths.length === 0) {
    throw new InvalidArgumentError("restore necesita al menos una ruta");
  }
  const { staged, worktree } = resolveRestoreTargets(options.staged, options.worktree);
  return [
    "restore",
    ...(worktree ? ["--worktree"] : []),
    ...(staged ? ["--staged"] : []),
    ...(options.source ? ["--source", options.source] : []),
    "--",
    ...paths,
  ];
}

function resolveRestoreTargets(
  staged: boolean | undefined,
  worktree: boolean | undefined,
): { staged: boolean; worktree: boolean } {
  let resolvedStaged = staged;
  let resolvedWorktree = worktree;
  if (staged === undefined && worktree === undefined) {
    resolvedWorktree = true;
  } else if (staged === true && worktree === undefined) {
    resolvedWorktree = false;
  } else if (staged === undefined && worktree !== undefined) {
    resolvedStaged = false;
  }
  if (resolvedStaged !== true && resolvedWorktree !== true) {
    throw new InvalidArgumentError("restore necesita staged o worktree en true");
  }
  return { staged: resolvedStaged === true, worktree: resolvedWorktree === true };
}

export function initArgs(
  path: string,
  options: {
    readonly bare?: boolean | undefined;
    readonly initialBranch?: string | undefined;
  } = {},
): string[] {
  return [
    "init",
    ...(options.initialBranch ? ["--initial-branch", options.initialBranch] : []),
    ...(options.bare ? ["--bare"] : []),
    path,
  ];
}

export function remoteAddArgs(name: string, url: string, fetch: boolean): string[] {
  if (!name || !url) {
    throw new InvalidArgumentError("remoteAdd necesita el nombre y la URL del remoto");
  }
  return ["remote", "add", ...(fetch ? ["-f"] : []), name, url];
}

export function remoteListArgs(): string[] {
  return ["remote"];
}

/**
 * El remoto donde inyectar credenciales: el dado; si no, el único que haya;
 * con varios, `origin` si existe (como `resolve_remote_name` en Python).
 */
export function resolveRemoteName(remote: string | undefined, remotesOutput: string): string {
  if (remote) {
    return remote;
  }
  const remotes = remotesOutput
    .split("\n")
    .map((line) => line.trim())
    .filter((line) => line.length > 0);
  if (remotes.length === 1) {
    return remotes[0] as string;
  }
  if (remotes.includes("origin")) {
    return "origin";
  }
  throw new InvalidArgumentError(
    "git con username/password y varios remotos (ninguno origin) necesita remote",
  );
}

export function remoteSetUrlArgs(name: string, url: string): string[] {
  return ["remote", "set-url", name, url];
}

export function remoteGetUrlArgs(name: string): string[] {
  return ["remote", "get-url", name];
}

/** `remote add … || remote set-url …` (y `fetch` detrás si se pidió): el `overwrite` de E2B. */
export function remoteAddOverwriteCommand(
  path: string,
  name: string,
  url: string,
  fetch: boolean,
): string {
  const add = gitCommand(remoteAddArgs(name, url, fetch), path);
  const setUrl = gitCommand(remoteSetUrlArgs(name, url), path);
  const replace = `${add} || ${setUrl}`;
  return fetch ? `(${replace}) && ${gitCommand(["fetch", name], path)}` : replace;
}

export function remoteGetCommand(path: string, name: string): string {
  if (!name) {
    throw new InvalidArgumentError("remoteGet necesita el nombre del remoto");
  }
  return `${gitCommand(remoteGetUrlArgs(name), path)} || true`;
}

export function pushArgs(
  options: {
    readonly remote?: string | undefined;
    readonly branch?: string | undefined;
    readonly setUpstream?: boolean | undefined;
  } = {},
): string[] {
  const remote = options.remote;
  return [
    "push",
    ...((options.setUpstream ?? true) && remote ? ["--set-upstream"] : []),
    ...(remote ? [remote] : []),
    ...(options.branch ? [options.branch] : []),
  ];
}

export function pullArgs(
  options: { readonly remote?: string | undefined; readonly branch?: string | undefined } = {},
): string[] {
  return [
    "pull",
    ...(options.remote ? [options.remote] : []),
    ...(options.branch ? [options.branch] : []),
  ];
}

export function hasUpstreamArgs(): string[] {
  return ["rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"];
}

export function validateConfigScope(scope: unknown): GitConfigScope {
  if (typeof scope !== "string" || !(GIT_CONFIG_SCOPES as readonly string[]).includes(scope)) {
    throw new InvalidArgumentError(
      `el scope de git config debe ser uno de ${GIT_CONFIG_SCOPES.join(", ")}`,
    );
  }
  return scope as GitConfigScope;
}

/** `--local` exige el repositorio; `--global` y `--system` corren sin `-C`. */
export function resolveConfigScope(
  scope: unknown,
  path: string | undefined,
): { flag: string; repoPath: string | undefined } {
  const validated = validateConfigScope(scope);
  if (validated !== "local") {
    return { flag: `--${validated}`, repoPath: undefined };
  }
  if (!path) {
    throw new InvalidArgumentError('scope "local" necesita la ruta del repositorio');
  }
  return { flag: "--local", repoPath: path };
}

export function setConfigArgs(flag: string, key: string, value: string): string[] {
  if (!key) {
    throw new InvalidArgumentError("git config necesita la clave");
  }
  return ["config", flag, key, value];
}

export function getConfigCommand(flag: string, key: string, repoPath: string | undefined): string {
  if (!key) {
    throw new InvalidArgumentError("git config necesita la clave");
  }
  return `${gitCommand(["config", flag, "--get", key], repoPath)} || true`;
}

/** `printf %s '<cuatro líneas>' | git credential approve`: el helper `store` las guarda en `~/.git-credentials`. */
export function credentialApproveCommand(input: {
  readonly username: string;
  readonly password: string;
  readonly host?: string | undefined;
  readonly protocol?: string | undefined;
}): string {
  if (!input.username || !input.password) {
    throw new InvalidArgumentError("dangerouslyAuthenticate necesita username y password");
  }
  const lines = [
    `protocol=${(input.protocol ?? "https").trim() || "https"}`,
    `host=${(input.host ?? "github.com").trim() || "github.com"}`,
    `username=${input.username}`,
    `password=${input.password}`,
    "",
    "",
  ].join("\n");
  return `printf %s ${shellQuote(lines)} | ${gitCommand(["credential", "approve"])}`;
}

/** `urllib.parse.quote(value, safe="")`: como `encodeURIComponent` más `!'()*`. */
export function percentEncode(value: string): string {
  return encodeURIComponent(value).replace(
    RFC3986_EXTRA,
    (char) => `%${char.charCodeAt(0).toString(16).toUpperCase()}`,
  );
}

/** Sólo URLs http(s); usuario y password juntos o ninguno; ambos percent-encoded. */
export function withCredentials(
  url: string,
  username: string | undefined,
  password: string | undefined,
): string {
  if (!username && !password) {
    return url;
  }
  if (!username || !password) {
    throw new InvalidArgumentError("las credenciales de git necesitan username y password");
  }
  const match = HTTP_URL.exec(url);
  if (match === null) {
    throw new InvalidArgumentError("sólo las URLs http(s) admiten username/password");
  }
  const [, scheme, authority, rest] = match as unknown as [string, string, string, string];
  const host = authority.slice(authority.lastIndexOf("@") + 1);
  return `${scheme}${percentEncode(username)}:${percentEncode(password)}@${host}${rest}`;
}

export function stripCredentials(url: string): string {
  const match = HTTP_URL.exec(url);
  if (match === null) {
    return url;
  }
  const [, scheme, authority, rest] = match as unknown as [string, string, string, string];
  const at = authority.lastIndexOf("@");
  return at < 0 ? url : `${scheme}${authority.slice(at + 1)}${rest}`;
}

export function deriveRepoDirFromUrl(url: string): string | undefined {
  const withoutQuery = url.split(/[?#]/, 1)[0] ?? "";
  const trimmed = withoutQuery.replace(/\/+$/, "");
  const afterScheme = trimmed.replace(/^[a-z][a-z0-9+.-]*:\/\/[^/]*/i, "");
  const last = afterScheme.split(/[/:]/).pop();
  if (!last) {
    return undefined;
  }
  return last.endsWith(".git") ? last.slice(0, -4) || undefined : last;
}

/**
 * El clone de E2B: con credenciales y sin `dangerouslyStoreCredentials`, el
 * repo (el `path` o el derivado de la URL) recibe después la URL limpia; sin
 * ruta derivable es `InvalidArgumentError` antes de ejecutar nada.
 */
export function buildClonePlan(input: ClonePlanInput): ClonePlan {
  if (input.password && !input.username) {
    throw new InvalidArgumentError("git clone con password o token necesita username");
  }
  const cloneUrl =
    input.username && input.password
      ? withCredentials(input.url, input.username, input.password)
      : input.url;
  const sanitized = stripCredentials(cloneUrl);
  const strip = !(input.dangerouslyStoreCredentials ?? false) && sanitized !== cloneUrl;
  const repoPath = strip ? (input.path ?? deriveRepoDirFromUrl(input.url)) : input.path;
  if (strip && !repoPath) {
    throw new InvalidArgumentError(
      "clone con credenciales necesita una ruta de destino si no se guardan",
    );
  }
  const args = [
    "clone",
    cloneUrl,
    ...(input.branch ? ["--branch", input.branch, "--single-branch"] : []),
    ...(input.depth ? ["--depth", String(input.depth)] : []),
    ...(input.path ? [input.path] : []),
  ];
  return { args, repoPath, sanitizedUrl: strip ? sanitized : undefined };
}

// ------------------------------------------------------------------ parsing

function parseAheadBehind(segment: string | undefined): { ahead: number; behind: number } {
  const count = (label: string): number => {
    const match = segment === undefined ? null : new RegExp(`${label}\\s+(\\d+)`).exec(segment);
    return match === null ? 0 : Number.parseInt(match[1] as string, 10);
  };
  return { ahead: count("ahead"), behind: count("behind") };
}

function normalizeBranchName(name: string): string {
  if (name.startsWith("HEAD (detached at ")) {
    return name.replace("HEAD (detached at ", "").replace(/\)$/, "");
  }
  return name
    .replace("HEAD (no branch)", "HEAD")
    .replace("No commits yet on ", "")
    .replace("Initial commit on ", "");
}

function deriveStatus(indexStatus: string, workingTreeStatus: string): GitStatusLabel {
  const codes = new Set([indexStatus, workingTreeStatus]);
  if (CONFLICT_CODES.has(`${indexStatus}${workingTreeStatus}`) || codes.has("U")) {
    return "conflict";
  }
  const ordered: ReadonlyArray<[string, GitStatusLabel]> = [
    ["R", "renamed"],
    ["C", "copied"],
    ["D", "deleted"],
    ["A", "added"],
    ["M", "modified"],
    ["T", "typechange"],
    ["?", "untracked"],
  ];
  return ordered.find(([code]) => codes.has(code))?.[1] ?? "unknown";
}

interface BranchLine {
  readonly currentBranch: string | undefined;
  readonly upstream: string | undefined;
  readonly ahead: number;
  readonly behind: number;
  readonly detached: boolean;
}

const NO_BRANCH: BranchLine = {
  currentBranch: undefined,
  upstream: undefined,
  ahead: 0,
  behind: 0,
  detached: false,
};

function parseBranchLine(line: string | undefined): BranchLine {
  if (line === undefined) {
    return NO_BRANCH;
  }
  const info = line.slice(3);
  const bracket = info.indexOf(" [");
  const branchPart = bracket < 0 ? info : info.slice(0, bracket);
  const counters = parseAheadBehind(bracket < 0 ? undefined : info.slice(bracket + 2, -1));
  const normalized = normalizeBranchName(branchPart);
  if (branchPart.includes("detached") || normalized.startsWith("HEAD")) {
    return { ...NO_BRANCH, ...counters, detached: true };
  }
  if (normalized.includes("...")) {
    const [branch, upstream] = normalized.split("...");
    return {
      ...counters,
      currentBranch: branch || undefined,
      upstream: upstream || undefined,
      detached: false,
    };
  }
  return { ...NO_BRANCH, ...counters, currentBranch: normalized || undefined };
}

function parseFileLine(line: string): GitFileStatus | undefined {
  if (line.startsWith("?? ")) {
    return {
      name: line.slice(3),
      status: "untracked",
      indexStatus: "?",
      workingTreeStatus: "?",
      staged: false,
    };
  }
  if (line.length < 3) {
    return undefined;
  }
  const indexStatus = line[0] as string;
  const workingTreeStatus = line[1] as string;
  const path = line.slice(3);
  const arrow = path.indexOf(" -> ");
  const renamedFrom = arrow < 0 ? undefined : path.slice(0, arrow);
  const entry: GitFileStatus = {
    name: arrow < 0 ? path : path.slice(arrow + 4),
    status: deriveStatus(indexStatus, workingTreeStatus),
    indexStatus,
    workingTreeStatus,
    staged: indexStatus !== " " && indexStatus !== "?",
  };
  return renamedFrom === undefined ? entry : { ...entry, renamedFrom };
}

/** `git status --porcelain=1 -b`: la línea `## rama...upstream [ahead N, behind M]` y una por fichero. */
export function parseGitStatus(output: string): GitStatus {
  const lines = output
    .split("\n")
    .map((line) => line.replace(/\r$/, ""))
    .filter((line) => line.trim().length > 0);
  const hasBranchLine = lines[0]?.startsWith("## ") ?? false;
  const branch = parseBranchLine(hasBranchLine ? lines[0] : undefined);
  const files = (hasBranchLine ? lines.slice(1) : lines)
    .map(parseFileLine)
    .filter((entry): entry is GitFileStatus => entry !== undefined);
  const stagedCount = files.filter((entry) => entry.staged).length;
  const untrackedCount = files.filter((entry) => entry.status === "untracked").length;
  const conflictCount = files.filter((entry) => entry.status === "conflict").length;
  return Object.freeze({
    ...branch,
    fileStatus: Object.freeze(files),
    isClean: files.length === 0,
    hasChanges: files.length > 0,
    hasStaged: stagedCount > 0,
    hasUntracked: untrackedCount > 0,
    hasConflicts: conflictCount > 0,
    totalCount: files.length,
    stagedCount,
    unstagedCount: files.length - stagedCount,
    untrackedCount,
    conflictCount,
  });
}

/** `git branch --format=%(refname:short)\t%(HEAD)`: `*` en la segunda columna marca la actual. */
export function parseGitBranches(output: string): GitBranches {
  const rows = output
    .split("\n")
    .map((line) => line.trim())
    .filter((line) => line.length > 0)
    .map((line) => line.split("\t"));
  const current = rows.find((parts) => parts[1] === "*");
  return Object.freeze({
    branches: Object.freeze(rows.map((parts) => parts[0] as string)),
    currentBranch: current?.[0],
  });
}

// ---------------------------------------------------------- classification

function failureText(error: unknown): string | undefined {
  if (!(error instanceof CommandExitError)) {
    return undefined;
  }
  return `${error.stderr}\n${error.stdout}`.toLowerCase();
}

export function isAuthFailure(error: unknown): boolean {
  const text = failureText(error);
  return text !== undefined && AUTH_FAILURE_SNIPPETS.some((snippet) => text.includes(snippet));
}

export function isMissingUpstream(error: unknown): boolean {
  const text = failureText(error);
  return text !== undefined && MISSING_UPSTREAM_SNIPPETS.some((snippet) => text.includes(snippet));
}

export function authErrorMessage(action: GitAction, missingPassword: boolean): string {
  return missingPassword
    ? `git ${action} necesita un password/token para repositorios privados`
    : `git ${action} necesita credenciales para repositorios privados`;
}

export function upstreamErrorMessage(action: "push" | "pull"): string {
  if (action === "push") {
    return (
      "git push falló porque la rama no tiene upstream configurado: fíjalo una vez con " +
      "setUpstream (y remote/branch si hace falta) o pasa remote y branch explícitos"
    );
  }
  return (
    "git pull falló porque la rama no tiene upstream configurado: pasa remote y branch " +
    "explícitos o fija el upstream una vez (push con setUpstream, o " +
    "git branch --set-upstream-to=origin/<rama> <rama>)"
  );
}

/** Cada secreto no vacío y su forma percent-encoded pasan a `***`. */
export function redact(text: string, secrets: readonly (string | undefined)[]): string {
  const needles = [
    ...new Set(secrets.flatMap((secret) => (secret ? [secret, percentEncode(secret)] : []))),
  ].sort((a, b) => b.length - a.length);
  return needles.reduce((current, needle) => current.split(needle).join(REDACTED), text);
}

/** Un `CommandExitError` nuevo, sin `cause`, con salida y mensaje redactados. */
export function redactedExitError(
  error: CommandExitError,
  secrets: readonly (string | undefined)[],
): CommandExitError {
  return new CommandExitError(redact(error.message, secrets), {
    exitCode: error.exitCode,
    stdout: redact(error.stdout, secrets),
    stderr: redact(error.stderr, secrets),
    error: error.error === undefined ? undefined : redact(error.error, secrets),
    grpcCode: error.grpcCode,
  });
}
