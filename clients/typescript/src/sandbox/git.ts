/**
 * `sandbox.git`: el módulo git de E2B 2.x sobre `commands.run` (sin RPC
 * nuevo). Cada operación es un `git [-C <ruta>] <args…>` entre comillas, en
 * primer plano y con `GIT_TERMINAL_PROMPT=0`. Nunca registra nada: el comando
 * lleva las credenciales cuando se pasan, y sólo viaja en `StartRequest.cmd`.
 */

import {
  CommandExitError,
  GitAuthError,
  GitUpstreamError,
  InvalidArgumentError,
} from "../errors.js";
import type { CommandResult } from "../models.js";
import type { CommandOptions } from "./commands.js";
import {
  addArgs,
  authErrorMessage,
  branchesArgs,
  buildClonePlan,
  checkoutBranchArgs,
  commitArgs,
  createBranchArgs,
  credentialApproveCommand,
  deleteBranchArgs,
  type GitAction,
  type GitBranches,
  type GitConfigScope,
  type GitResetMode,
  type GitStatus,
  getConfigCommand,
  gitCommand,
  gitEnvs,
  hasUpstreamArgs,
  initArgs,
  isAuthFailure,
  isMissingUpstream,
  parseGitBranches,
  parseGitStatus,
  pullArgs,
  pushArgs,
  redactedExitError,
  remoteAddArgs,
  remoteAddOverwriteCommand,
  remoteGetCommand,
  remoteGetUrlArgs,
  remoteListArgs,
  remoteSetUrlArgs,
  resetArgs,
  resolveConfigScope,
  resolveRemoteName,
  restoreArgs,
  setConfigArgs,
  statusArgs,
  upstreamErrorMessage,
  withCredentials,
} from "./git-args.js";

export const DEFAULT_GIT_REMOTE = "origin";

export type GitRunOptions = CommandOptions & { readonly background?: false | undefined };

/** Lo único que `Git` necesita del sandbox: `commands.run` en primer plano. */
export interface GitCommandRunner {
  run(cmd: string, options: GitRunOptions): Promise<CommandResult>;
}

/** `timeoutMs` ausente es sin deadline del servidor (el git de E2B pasa `None`). */
export interface GitRequestOpts {
  readonly envs?: Readonly<Record<string, string>> | undefined;
  readonly user?: string | undefined;
  readonly cwd?: string | undefined;
  readonly timeoutMs?: number | undefined;
  readonly requestTimeoutMs?: number | undefined;
  readonly signal?: AbortSignal | undefined;
}

export interface GitCloneOpts extends GitRequestOpts {
  readonly path?: string | undefined;
  readonly branch?: string | undefined;
  readonly depth?: number | undefined;
  readonly username?: string | undefined;
  readonly password?: string | undefined;
  readonly dangerouslyStoreCredentials?: boolean | undefined;
}

export interface GitInitOpts extends GitRequestOpts {
  readonly bare?: boolean | undefined;
  readonly initialBranch?: string | undefined;
}

export interface GitRemoteAddOpts extends GitRequestOpts {
  readonly fetch?: boolean | undefined;
  readonly overwrite?: boolean | undefined;
}

export interface GitDeleteBranchOpts extends GitRequestOpts {
  readonly force?: boolean | undefined;
}

export interface GitAddOpts extends GitRequestOpts {
  readonly files?: readonly string[] | undefined;
  readonly all?: boolean | undefined;
}

export interface GitCommitOpts extends GitRequestOpts {
  readonly authorName?: string | undefined;
  readonly authorEmail?: string | undefined;
  readonly allowEmpty?: boolean | undefined;
}

export interface GitResetOpts extends GitRequestOpts {
  readonly mode?: GitResetMode | undefined;
  readonly target?: string | undefined;
  readonly paths?: readonly string[] | undefined;
}

export interface GitRestoreOpts extends GitRequestOpts {
  readonly paths: readonly string[];
  readonly staged?: boolean | undefined;
  readonly worktree?: boolean | undefined;
  readonly source?: string | undefined;
}

export interface GitCredentialOpts extends GitRequestOpts {
  readonly remote?: string | undefined;
  readonly branch?: string | undefined;
  readonly username?: string | undefined;
  readonly password?: string | undefined;
}

export interface GitPushOpts extends GitCredentialOpts {
  readonly setUpstream?: boolean | undefined;
}

export type GitPullOpts = GitCredentialOpts;

export interface GitConfigOpts extends GitRequestOpts {
  readonly scope?: GitConfigScope | undefined;
  readonly path?: string | undefined;
}

export interface GitDangerouslyAuthenticateOpts extends GitRequestOpts {
  readonly username: string;
  readonly password: string;
  readonly host?: string | undefined;
  readonly protocol?: string | undefined;
}

function requestOptions(opts: GitRequestOpts | undefined): GitRunOptions {
  return {
    envs: gitEnvs(opts?.envs),
    user: opts?.user,
    cwd: opts?.cwd,
    timeoutMs: opts?.timeoutMs ?? 0,
    requestTimeoutMs: opts?.requestTimeoutMs,
    signal: opts?.signal,
  };
}

function requireUsernameForPassword(action: GitAction, opts: GitCredentialOpts | GitCloneOpts) {
  if (opts.password && !opts.username) {
    throw new InvalidArgumentError(`git ${action} con password o token necesita username`);
  }
}

/**
 * El mapeo de fallos de E2B: autenticación → `GitAuthError` (sin la URL);
 * sin upstream (sólo push/pull) → `GitUpstreamError`; lo demás, el mismo
 * `CommandExitError`, redactado y sin `cause` cuando llevaba credenciales.
 */
function gitFailure(
  error: unknown,
  action: GitAction | undefined,
  credentials: { readonly username?: string | undefined; readonly password?: string | undefined },
): unknown {
  if (!(error instanceof CommandExitError)) {
    return error;
  }
  if (action !== undefined && isAuthFailure(error)) {
    return new GitAuthError(
      authErrorMessage(action, Boolean(credentials.username) && !credentials.password),
    );
  }
  if ((action === "push" || action === "pull") && isMissingUpstream(error)) {
    return new GitUpstreamError(upstreamErrorMessage(action));
  }
  return credentials.password ? redactedExitError(error, [credentials.password]) : error;
}

/**
 * Git del sandbox con los métodos y modelos de E2B. **`dangerouslyAuthenticate`
 * deja las credenciales en `~/.git-credentials` del usuario del sandbox,
 * legibles por cualquier código que corra en él.**
 */
export class Git {
  readonly #commands: GitCommandRunner;

  constructor(commands: GitCommandRunner) {
    this.#commands = commands;
  }

  async clone(url: string, opts: GitCloneOpts = {}): Promise<CommandResult> {
    requireUsernameForPassword("clone", opts);
    const plan = buildClonePlan({ url, ...opts });
    try {
      const result = await this.#runGit(plan.args, undefined, opts);
      if (plan.sanitizedUrl !== undefined) {
        await this.#runGit(
          remoteSetUrlArgs(DEFAULT_GIT_REMOTE, plan.sanitizedUrl),
          plan.repoPath,
          opts,
        );
      }
      return result;
    } catch (error) {
      throw gitFailure(error, "clone", opts);
    }
  }

  init(path: string, opts: GitInitOpts = {}): Promise<CommandResult> {
    return this.#runGit(initArgs(path, opts), undefined, opts);
  }

  /** Con `fetch` habla con el remoto: un fallo de autenticación es `GitAuthError`. */
  async remoteAdd(
    path: string,
    name: string,
    url: string,
    opts: GitRemoteAddOpts = {},
  ): Promise<CommandResult> {
    const fetch = opts.fetch ?? false;
    const run = opts.overwrite
      ? () => this.#runShell(remoteAddOverwriteCommand(path, name, url, fetch), opts)
      : () => this.#runGit(remoteAddArgs(name, url, fetch), path, opts);
    try {
      return await run();
    } catch (error) {
      throw gitFailure(error, fetch ? "remote add" : undefined, {});
    }
  }

  async remoteGet(
    path: string,
    name: string,
    opts: GitRequestOpts = {},
  ): Promise<string | undefined> {
    const result = await this.#runShell(remoteGetCommand(path, name), opts);
    return result.stdout.trim() || undefined;
  }

  async status(path: string, opts: GitRequestOpts = {}): Promise<GitStatus> {
    return parseGitStatus((await this.#runGit(statusArgs(), path, opts)).stdout);
  }

  async branches(path: string, opts: GitRequestOpts = {}): Promise<GitBranches> {
    return parseGitBranches((await this.#runGit(branchesArgs(), path, opts)).stdout);
  }

  createBranch(path: string, branch: string, opts: GitRequestOpts = {}): Promise<CommandResult> {
    return this.#runGit(createBranchArgs(branch), path, opts);
  }

  checkoutBranch(path: string, branch: string, opts: GitRequestOpts = {}): Promise<CommandResult> {
    return this.#runGit(checkoutBranchArgs(branch), path, opts);
  }

  deleteBranch(
    path: string,
    branch: string,
    opts: GitDeleteBranchOpts = {},
  ): Promise<CommandResult> {
    return this.#runGit(deleteBranchArgs(branch, opts.force ?? false), path, opts);
  }

  add(path: string, opts: GitAddOpts = {}): Promise<CommandResult> {
    return this.#runGit(addArgs(opts.files, opts.all ?? true), path, opts);
  }

  commit(path: string, message: string, opts: GitCommitOpts = {}): Promise<CommandResult> {
    return this.#runGit(commitArgs(message, opts), path, opts);
  }

  reset(path: string, opts: GitResetOpts = {}): Promise<CommandResult> {
    return this.#runGit(resetArgs(opts), path, opts);
  }

  restore(path: string, opts: GitRestoreOpts): Promise<CommandResult> {
    return this.#runGit(restoreArgs(opts.paths, opts), path, opts);
  }

  async push(path: string, opts: GitPushOpts = {}): Promise<CommandResult> {
    requireUsernameForPassword("push", opts);
    if (opts.username && opts.password) {
      const remote = await this.#remoteName(path, opts);
      return this.#withRemoteCredentials(path, remote, opts, "push", () =>
        this.#runGit(pushArgs({ ...opts, remote }), path, opts),
      );
    }
    try {
      return await this.#runGit(pushArgs(opts), path, opts);
    } catch (error) {
      throw gitFailure(error, "push", opts);
    }
  }

  async pull(path: string, opts: GitPullOpts = {}): Promise<CommandResult> {
    requireUsernameForPassword("pull", opts);
    if (!opts.remote && !opts.branch && !(await this.#hasUpstream(path, opts))) {
      throw new GitUpstreamError(upstreamErrorMessage("pull"));
    }
    if (opts.username && opts.password) {
      const remote = await this.#remoteName(path, opts);
      return this.#withRemoteCredentials(path, remote, opts, "pull", () =>
        this.#runGit(pullArgs({ ...opts, remote }), path, opts),
      );
    }
    try {
      return await this.#runGit(pullArgs(opts), path, opts);
    } catch (error) {
      throw gitFailure(error, "pull", opts);
    }
  }

  setConfig(key: string, value: string, opts: GitConfigOpts = {}): Promise<CommandResult> {
    const { flag, repoPath } = resolveConfigScope(opts.scope ?? "global", opts.path);
    return this.#runGit(setConfigArgs(flag, key, value), repoPath, opts);
  }

  async getConfig(key: string, opts: GitConfigOpts = {}): Promise<string | undefined> {
    const { flag, repoPath } = resolveConfigScope(opts.scope ?? "global", opts.path);
    const result = await this.#runShell(getConfigCommand(flag, key, repoPath), opts);
    return result.stdout.trim() || undefined;
  }

  /**
   * `credential.helper=store` global y `git credential approve`. **Las
   * credenciales quedan en `~/.git-credentials` (modo 0600, del usuario del
   * sandbox) y cualquier código del sandbox puede leerlas.**
   */
  async dangerouslyAuthenticate(opts: GitDangerouslyAuthenticateOpts): Promise<CommandResult> {
    const approve = credentialApproveCommand(opts);
    await this.setConfig("credential.helper", "store", { ...opts, scope: "global" });
    try {
      return await this.#runShell(approve, opts);
    } catch (error) {
      throw gitFailure(error, undefined, opts);
    }
  }

  async configureUser(
    name: string,
    email: string,
    opts: GitConfigOpts = {},
  ): Promise<CommandResult> {
    if (!name || !email) {
      throw new InvalidArgumentError("configureUser necesita name y email");
    }
    await this.setConfig("user.name", name, opts);
    return this.setConfig("user.email", email, opts);
  }

  /**
   * `remote get-url` → `remote set-url` con credenciales → la operación →
   * `remote set-url` con la URL original, siempre (también si la operación
   * falló). Un fallo al restaurar sólo se lanza si la operación salió bien.
   */
  async #withRemoteCredentials(
    path: string,
    remote: string,
    opts: GitCredentialOpts,
    action: "push" | "pull",
    operation: () => Promise<CommandResult>,
  ): Promise<CommandResult> {
    const original = await this.#remoteUrl(path, remote, opts);
    const credentialed = withCredentials(original, opts.username, opts.password);
    const outcome = await this.#settle(async () => {
      await this.#runGit(remoteSetUrlArgs(remote, credentialed), path, opts);
      return operation();
    });
    const restored = await this.#settle(() =>
      this.#runGit(remoteSetUrlArgs(remote, original), path, opts),
    );
    if (outcome.failed) {
      throw gitFailure(outcome.error, action, opts);
    }
    if (restored.failed) {
      throw gitFailure(restored.error, action, opts);
    }
    return outcome.value;
  }

  async #settle(
    operation: () => Promise<CommandResult>,
  ): Promise<
    | { readonly failed: false; readonly value: CommandResult }
    | { readonly failed: true; readonly error: unknown }
  > {
    try {
      return { failed: false, value: await operation() };
    } catch (error) {
      return { failed: true, error };
    }
  }

  /** Con credenciales y sin `remote`, `git remote` decide: el único remoto, o `origin` entre varios. */
  async #remoteName(path: string, opts: GitCredentialOpts): Promise<string> {
    if (opts.remote) {
      return opts.remote;
    }
    try {
      return resolveRemoteName(
        undefined,
        (await this.#runGit(remoteListArgs(), path, opts)).stdout,
      );
    } catch (error) {
      throw gitFailure(error, undefined, opts);
    }
  }

  async #remoteUrl(path: string, remote: string, opts: GitRequestOpts): Promise<string> {
    const url = (await this.#runGit(remoteGetUrlArgs(remote), path, opts)).stdout.trim();
    if (!url) {
      throw new InvalidArgumentError(`el remoto "${remote}" no tiene URL en el repositorio`);
    }
    return url;
  }

  async #hasUpstream(path: string, opts: GitRequestOpts): Promise<boolean> {
    try {
      return (await this.#runGit(hasUpstreamArgs(), path, opts)).stdout.trim().length > 0;
    } catch (error) {
      if (error instanceof CommandExitError) {
        return false;
      }
      throw error;
    }
  }

  #runGit(args: readonly string[], repoPath: string | undefined, opts: GitRequestOpts | undefined) {
    return this.#commands.run(gitCommand(args, repoPath), requestOptions(opts));
  }

  #runShell(command: string, opts: GitRequestOpts | undefined) {
    return this.#commands.run(command, requestOptions(opts));
  }
}
