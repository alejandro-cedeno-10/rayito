/**
 * `sandbox.git` (D15): los argv exactos de E2B, los parsers de porcelain, las
 * credenciales en la URL y su redacción, la clasificación de fallos y el
 * orden get-url → set-url(creds) → operación → set-url(original), también
 * cuando la operación sale con 128. El espejo de `test_git_base.py` y
 * `test_git_sync.py`.
 */

import { describe, expect, test } from "vitest";
import {
  AuthenticationError,
  CommandExitError,
  GitAuthError,
  GitUpstreamError,
  InvalidArgumentError,
  SandboxError,
} from "../../src/errors.js";
import { Git as ExportedGit, GitAuthError as ExportedGitAuthError } from "../../src/index.js";
import type { CommandResult } from "../../src/models.js";
import { Git, type GitRunOptions } from "../../src/sandbox/git.js";
import {
  addArgs,
  branchesArgs,
  buildClonePlan,
  checkoutBranchArgs,
  commitArgs,
  createBranchArgs,
  credentialApproveCommand,
  deleteBranchArgs,
  deriveRepoDirFromUrl,
  gitCommand,
  gitEnvs,
  initArgs,
  isAuthFailure,
  isMissingUpstream,
  parseGitBranches,
  parseGitStatus,
  percentEncode,
  pullArgs,
  pushArgs,
  redact,
  remoteAddArgs,
  remoteAddOverwriteCommand,
  resetArgs,
  resolveConfigScope,
  resolveRemoteName,
  restoreArgs,
  shellQuote,
  statusArgs,
  stripCredentials,
  withCredentials,
} from "../../src/sandbox/git-args.js";
import { createTestSandbox } from "./helpers.js";

const PASSWORD = "s3cr:t@tok/en";
const ENCODED_PASSWORD = "s3cr%3At%40tok%2Fen";
const REPO = "/home/user/repo";
const ORIGIN_URL = "https://github.com/acme/app.git";

interface RecordedRun {
  readonly cmd: string;
  readonly options: GitRunOptions;
}

type Responder = (cmd: string, index: number) => Partial<CommandResult> | Error;

class RecordingRunner {
  readonly runs: RecordedRun[] = [];
  readonly #respond: Responder;

  constructor(respond: Responder = () => ({})) {
    this.#respond = respond;
  }

  get commands(): string[] {
    return this.runs.map((run) => run.cmd);
  }

  async run(cmd: string, options: GitRunOptions): Promise<CommandResult> {
    const index = this.runs.length;
    this.runs.push({ cmd, options });
    const outcome = this.#respond(cmd, index);
    if (outcome instanceof Error) {
      throw outcome;
    }
    return { stdout: "", stderr: "", exitCode: 0, error: undefined, ...outcome };
  }
}

function exit128(stderr: string, stdout = ""): CommandExitError {
  return new CommandExitError(`el comando terminó con código 128: ${stderr}`, {
    exitCode: 128,
    stdout,
    stderr,
  });
}

function git(runner: RecordingRunner): Git {
  return new Git(runner);
}

/** Un remoto `origin` con URL y todo lo demás en 0, salvo lo que diga `fail`. */
function remoteResponder(fail?: (cmd: string) => Error | undefined): Responder {
  return (cmd) => {
    const failure = fail?.(cmd);
    if (failure !== undefined) {
      return failure;
    }
    if (cmd.includes("'remote' 'get-url'")) {
      return { stdout: `${ORIGIN_URL}\n` };
    }
    if (cmd.endsWith("'remote'")) {
      return { stdout: "origin\n" };
    }
    return {};
  };
}

describe("git-args: quoting and the E2B argv table", () => {
  test("shellQuote always quotes and escapes single quotes", () => {
    expect(shellQuote("plain")).toBe("'plain'");
    expect(shellQuote("it's")).toBe(`'it'"'"'s'`);
    expect(gitCommand(["status"], REPO)).toBe(`'git' '-C' '${REPO}' 'status'`);
    expect(gitCommand(["status"])).toBe("'git' 'status'");
  });

  test("every builder produces E2B's argv element by element", () => {
    const table: ReadonlyArray<[string[], string[]]> = [
      [statusArgs(), ["status", "--porcelain=1", "-b"]],
      [branchesArgs(), ["branch", "--format=%(refname:short)\t%(HEAD)"]],
      [createBranchArgs("f"), ["checkout", "-b", "f"]],
      [checkoutBranchArgs("f"), ["checkout", "f"]],
      [deleteBranchArgs("f", false), ["branch", "-d", "f"]],
      [deleteBranchArgs("f", true), ["branch", "-D", "f"]],
      [addArgs(undefined, true), ["add", "-A"]],
      [addArgs([], false), ["add", "."]],
      [addArgs(["a", "b"], true), ["add", "--", "a", "b"]],
      [commitArgs("m"), ["commit", "-m", "m"]],
      [
        commitArgs("m", { authorName: "Ana", authorEmail: "ana@example.com", allowEmpty: true }),
        [
          "-c",
          "user.name=Ana",
          "-c",
          "user.email=ana@example.com",
          "commit",
          "-m",
          "m",
          "--allow-empty",
        ],
      ],
      [resetArgs({ mode: "hard", target: "HEAD~1" }), ["reset", "--hard", "HEAD~1"]],
      [resetArgs({ paths: ["x"] }), ["reset", "--", "x"]],
      [restoreArgs(["x"]), ["restore", "--worktree", "--", "x"]],
      [restoreArgs(["x"], { staged: true }), ["restore", "--staged", "--", "x"]],
      [
        restoreArgs(["x"], { staged: true, worktree: true, source: "HEAD" }),
        ["restore", "--worktree", "--staged", "--source", "HEAD", "--", "x"],
      ],
      [initArgs("/r"), ["init", "/r"]],
      [
        initArgs("/r", { bare: true, initialBranch: "main" }),
        ["init", "--initial-branch", "main", "--bare", "/r"],
      ],
      [remoteAddArgs("origin", ORIGIN_URL, true), ["remote", "add", "-f", "origin", ORIGIN_URL]],
      [
        pushArgs({ remote: "origin", branch: "main" }),
        ["push", "--set-upstream", "origin", "main"],
      ],
      [pushArgs({ remote: "origin", setUpstream: false }), ["push", "origin"]],
      [pushArgs(), ["push"]],
      [pullArgs({ remote: "origin", branch: "main" }), ["pull", "origin", "main"]],
      [pullArgs(), ["pull"]],
    ];
    for (const [actual, expected] of table) {
      expect(actual).toEqual(expected);
    }
  });

  test("invalid arguments are refused before anything runs", () => {
    expect(() => resetArgs({ mode: "wipe" as never })).toThrow(InvalidArgumentError);
    expect(() => restoreArgs([])).toThrow(InvalidArgumentError);
    expect(() => restoreArgs(["x"], { staged: false, worktree: false })).toThrow(
      InvalidArgumentError,
    );
    expect(() => resolveConfigScope("local", undefined)).toThrow(InvalidArgumentError);
    expect(resolveConfigScope("global", REPO)).toEqual({ flag: "--global", repoPath: undefined });
    expect(resolveConfigScope("local", REPO)).toEqual({ flag: "--local", repoPath: REPO });
  });

  test("remote add with overwrite falls back to set-url, then fetches", () => {
    expect(remoteAddOverwriteCommand(REPO, "origin", ORIGIN_URL, true)).toBe(
      `('git' '-C' '${REPO}' 'remote' 'add' '-f' 'origin' '${ORIGIN_URL}' || ` +
        `'git' '-C' '${REPO}' 'remote' 'set-url' 'origin' '${ORIGIN_URL}') && ` +
        `'git' '-C' '${REPO}' 'fetch' 'origin'`,
    );
  });

  test("GIT_TERMINAL_PROMPT=0 sits under the caller's envs", () => {
    expect(gitEnvs(undefined)).toEqual({ GIT_TERMINAL_PROMPT: "0" });
    expect(gitEnvs({ GIT_TERMINAL_PROMPT: "1", K: "v" })).toEqual({
      GIT_TERMINAL_PROMPT: "1",
      K: "v",
    });
  });
});

describe("git-args: porcelain parsers", () => {
  test("clean branch with upstream and counters", () => {
    const status = parseGitStatus("## main...origin/main [ahead 2, behind 1]\n");
    expect(status).toMatchObject({
      currentBranch: "main",
      upstream: "origin/main",
      ahead: 2,
      behind: 1,
      detached: false,
      isClean: true,
      hasChanges: false,
      totalCount: 0,
    });
  });

  test("detached HEAD and unborn branch", () => {
    expect(parseGitStatus("## HEAD (no branch)\n")).toMatchObject({
      detached: true,
      currentBranch: undefined,
    });
    expect(parseGitStatus("## No commits yet on main\n")).toMatchObject({
      currentBranch: "main",
      detached: false,
    });
  });

  test("rename, conflict and untracked entries with their counters", () => {
    const status = parseGitStatus(
      ["## main", "R  old.txt -> new.txt", "UU both.txt", " M work.txt", "?? fresh.txt"].join("\n"),
    );
    expect(status.fileStatus).toEqual([
      {
        name: "new.txt",
        status: "renamed",
        indexStatus: "R",
        workingTreeStatus: " ",
        staged: true,
        renamedFrom: "old.txt",
      },
      {
        name: "both.txt",
        status: "conflict",
        indexStatus: "U",
        workingTreeStatus: "U",
        staged: true,
      },
      {
        name: "work.txt",
        status: "modified",
        indexStatus: " ",
        workingTreeStatus: "M",
        staged: false,
      },
      {
        name: "fresh.txt",
        status: "untracked",
        indexStatus: "?",
        workingTreeStatus: "?",
        staged: false,
      },
    ]);
    expect(status).toMatchObject({
      isClean: false,
      hasConflicts: true,
      hasUntracked: true,
      hasStaged: true,
      totalCount: 4,
      stagedCount: 2,
      unstagedCount: 2,
      untrackedCount: 1,
      conflictCount: 1,
    });
  });

  test("branches mark the current one", () => {
    expect(parseGitBranches("main\t*\nfeature\t \n")).toEqual({
      branches: ["main", "feature"],
      currentBranch: "main",
    });
    expect(parseGitBranches("")).toEqual({ branches: [], currentBranch: undefined });
  });
});

describe("git-args: credentials, redaction and classification", () => {
  test("credentials are percent-encoded like urllib.parse.quote(safe='')", () => {
    expect(percentEncode(PASSWORD)).toBe(ENCODED_PASSWORD);
    expect(percentEncode("a!b'c(d)e*")).toBe("a%21b%27c%28d%29e%2A");
    expect(withCredentials(ORIGIN_URL, "bot", PASSWORD)).toBe(
      `https://bot:${ENCODED_PASSWORD}@github.com/acme/app.git`,
    );
    expect(stripCredentials(`https://bot:${ENCODED_PASSWORD}@github.com/acme/app.git`)).toBe(
      ORIGIN_URL,
    );
    expect(() => withCredentials("git@github.com:acme/app.git", "bot", PASSWORD)).toThrow(
      InvalidArgumentError,
    );
    expect(() => withCredentials(ORIGIN_URL, "bot", undefined)).toThrow(InvalidArgumentError);
  });

  test("redact replaces the raw and the encoded password", () => {
    const text = `fatal: https://bot:${ENCODED_PASSWORD}@github.com and ${PASSWORD}`;
    const redacted = redact(text, [PASSWORD]);
    expect(redacted).not.toContain(PASSWORD);
    expect(redacted).not.toContain(ENCODED_PASSWORD);
    expect(redacted).toContain("***");
  });

  test("the snippet tables classify auth and upstream failures", () => {
    for (const stderr of [
      "fatal: Authentication failed for 'https://github.com/'",
      "fatal: could not read Username for 'https://github.com': terminal prompts disabled",
      "remote: Permission denied",
    ]) {
      expect(isAuthFailure(exit128(stderr))).toBe(true);
    }
    expect(isMissingUpstream(exit128("fatal: The current branch f has no upstream branch."))).toBe(
      true,
    );
    expect(
      isMissingUpstream(exit128("", "There is no tracking information for the current branch.")),
    ).toBe(true);
    expect(isAuthFailure(exit128("fatal: not a git repository"))).toBe(false);
    expect(isAuthFailure(new SandboxError("Authentication failed"))).toBe(false);
  });

  test("the clone plan strips credentials into the derived repo, or refuses without a path", () => {
    const plan = buildClonePlan({ url: ORIGIN_URL, username: "bot", password: PASSWORD });
    expect(plan.args).toEqual(["clone", `https://bot:${ENCODED_PASSWORD}@github.com/acme/app.git`]);
    expect(plan.repoPath).toBe("app");
    expect(plan.sanitizedUrl).toBe(ORIGIN_URL);
    expect(
      buildClonePlan({
        url: ORIGIN_URL,
        username: "bot",
        password: PASSWORD,
        dangerouslyStoreCredentials: true,
      }).sanitizedUrl,
    ).toBeUndefined();
    expect(deriveRepoDirFromUrl("https://github.com/")).toBeUndefined();
    expect(() =>
      buildClonePlan({ url: "https://github.com/", username: "bot", password: PASSWORD }),
    ).toThrow(InvalidArgumentError);
    expect(
      buildClonePlan({ url: ORIGIN_URL, branch: "dev", depth: 1, path: "/w/app" }).args,
    ).toEqual([
      "clone",
      ORIGIN_URL,
      "--branch",
      "dev",
      "--single-branch",
      "--depth",
      "1",
      "/w/app",
    ]);
  });

  test("the remote for credentials is the given one, the only one, or origin", () => {
    expect(resolveRemoteName("upstream", "")).toBe("upstream");
    expect(resolveRemoteName(undefined, "fork\n")).toBe("fork");
    expect(resolveRemoteName(undefined, "fork\norigin\n")).toBe("origin");
    expect(() => resolveRemoteName(undefined, "a\nb\n")).toThrow(InvalidArgumentError);
  });

  test("the credential approve command pipes the four lines", () => {
    expect(credentialApproveCommand({ username: "bot", password: "tok" })).toBe(
      `printf %s 'protocol=https\nhost=github.com\nusername=bot\npassword=tok\n\n' | 'git' 'credential' 'approve'`,
    );
  });
});

describe("Git over commands.run", () => {
  test("each method runs the exact command in the foreground with GIT_TERMINAL_PROMPT=0", async () => {
    const runner = new RecordingRunner((cmd) =>
      cmd.includes("'status'") ? { stdout: "## main\n" } : { stdout: "main\t*\n" },
    );
    const subject = git(runner);
    const status = await subject.status(REPO, { envs: { K: "v" }, user: "root", cwd: "/tmp" });
    expect(status.currentBranch).toBe("main");
    await subject.branches(REPO);
    await subject.createBranch(REPO, "f");
    await subject.commit(REPO, "it's done", { allowEmpty: true });
    expect(runner.commands).toEqual([
      `'git' '-C' '${REPO}' 'status' '--porcelain=1' '-b'`,
      `'git' '-C' '${REPO}' 'branch' '--format=%(refname:short)\t%(HEAD)'`,
      `'git' '-C' '${REPO}' 'checkout' '-b' 'f'`,
      `'git' '-C' '${REPO}' 'commit' '-m' 'it'"'"'s done' '--allow-empty'`,
    ]);
    expect(runner.runs[0]?.options).toMatchObject({
      envs: { GIT_TERMINAL_PROMPT: "0", K: "v" },
      user: "root",
      cwd: "/tmp",
      timeoutMs: 0,
    });
    expect(runner.runs.every((run) => run.options.background === undefined)).toBe(true);
    expect(runner.runs[1]?.options.envs).toEqual({ GIT_TERMINAL_PROMPT: "0" });
  });

  test("push with credentials restores the original URL even when push exits 128", async () => {
    const runner = new RecordingRunner(
      remoteResponder((cmd) =>
        cmd.includes("'push'")
          ? exit128(`fatal: unable to access 'https://bot:${ENCODED_PASSWORD}@github.com/': 500`)
          : undefined,
      ),
    );
    const error = await git(runner)
      .push(REPO, { branch: "main", username: "bot", password: PASSWORD })
      .catch((caught: unknown) => caught);
    expect(runner.commands).toEqual([
      `'git' '-C' '${REPO}' 'remote'`,
      `'git' '-C' '${REPO}' 'remote' 'get-url' 'origin'`,
      `'git' '-C' '${REPO}' 'remote' 'set-url' 'origin' 'https://bot:${ENCODED_PASSWORD}@github.com/acme/app.git'`,
      `'git' '-C' '${REPO}' 'push' '--set-upstream' 'origin' 'main'`,
      `'git' '-C' '${REPO}' 'remote' 'set-url' 'origin' '${ORIGIN_URL}'`,
    ]);
    expect(error).toBeInstanceOf(CommandExitError);
    const failure = error as CommandExitError;
    const visible = `${failure.message}\n${failure.stderr}\n${failure.stdout}\n${String(failure.cause)}`;
    expect(visible).not.toContain(PASSWORD);
    expect(visible).not.toContain(ENCODED_PASSWORD);
    expect(failure.cause).toBeUndefined();
  });

  test("push with credentials and a single non-origin remote injects them there", async () => {
    const runner = new RecordingRunner((cmd) => {
      if (cmd.endsWith("'remote'")) {
        return { stdout: "fork\n" };
      }
      if (cmd.includes("'get-url'")) {
        return { stdout: `${ORIGIN_URL}\n` };
      }
      return {};
    });
    await git(runner).push(REPO, { username: "bot", password: PASSWORD });
    expect(runner.commands[1]).toBe(`'git' '-C' '${REPO}' 'remote' 'get-url' 'fork'`);
    expect(runner.commands[3]).toBe(`'git' '-C' '${REPO}' 'push' '--set-upstream' 'fork'`);
  });

  test("pull with credentials follows the same order", async () => {
    const runner = new RecordingRunner(remoteResponder());
    await git(runner).pull(REPO, {
      remote: "origin",
      branch: "main",
      username: "bot",
      password: PASSWORD,
    });
    expect(runner.commands).toEqual([
      `'git' '-C' '${REPO}' 'remote' 'get-url' 'origin'`,
      `'git' '-C' '${REPO}' 'remote' 'set-url' 'origin' 'https://bot:${ENCODED_PASSWORD}@github.com/acme/app.git'`,
      `'git' '-C' '${REPO}' 'pull' 'origin' 'main'`,
      `'git' '-C' '${REPO}' 'remote' 'set-url' 'origin' '${ORIGIN_URL}'`,
    ]);
  });

  test("an auth failure becomes GitAuthError without the URL", async () => {
    const runner = new RecordingRunner(() =>
      exit128(`fatal: could not read Username for '${ORIGIN_URL}': terminal prompts disabled`),
    );
    const error = await git(runner)
      .push(REPO, { remote: "origin" })
      .catch((caught: unknown) => caught);
    expect(error).toBeInstanceOf(GitAuthError);
    expect(error).toBeInstanceOf(AuthenticationError);
    expect((error as Error).message).toBe(
      "git push necesita credenciales para repositorios privados",
    );
    expect((error as Error).message).not.toContain("github.com");
    const missing = await git(new RecordingRunner(() => exit128("Authentication failed")))
      .clone(ORIGIN_URL, { username: "bot" })
      .catch((caught: unknown) => caught);
    expect((missing as Error).message).toBe(
      "git clone necesita un password/token para repositorios privados",
    );
  });

  test("remote add with fetch classifies auth failures; without fetch it does not", async () => {
    const failing = () => new RecordingRunner(() => exit128("fatal: Authentication failed"));
    await expect(
      git(failing()).remoteAdd(REPO, "origin", ORIGIN_URL, { fetch: true }),
    ).rejects.toBeInstanceOf(GitAuthError);
    await expect(git(failing()).remoteAdd(REPO, "origin", ORIGIN_URL)).rejects.toBeInstanceOf(
      CommandExitError,
    );
  });

  test("a missing upstream on push becomes GitUpstreamError, and pull checks first", async () => {
    const push = await git(
      new RecordingRunner(() => exit128("fatal: The current branch f has no upstream branch.")),
    )
      .push(REPO, { setUpstream: false })
      .catch((caught: unknown) => caught);
    expect(push).toBeInstanceOf(GitUpstreamError);
    expect(push).toBeInstanceOf(SandboxError);
    const runner = new RecordingRunner(() =>
      exit128("fatal: no upstream configured for branch 'f'"),
    );
    const pull = await git(runner)
      .pull(REPO)
      .catch((caught: unknown) => caught);
    expect(pull).toBeInstanceOf(GitUpstreamError);
    expect(runner.commands).toEqual([
      `'git' '-C' '${REPO}' 'rev-parse' '--abbrev-ref' '--symbolic-full-name' '@{u}'`,
    ]);
  });

  test("a failing clone with credentials leaks the password nowhere", async () => {
    const runner = new RecordingRunner(() =>
      exit128(
        `fatal: repository 'https://bot:${ENCODED_PASSWORD}@github.com/acme/app.git/' not found`,
        PASSWORD,
      ),
    );
    const error = await git(runner)
      .clone(ORIGIN_URL, { username: "bot", password: PASSWORD })
      .catch((caught: unknown) => caught);
    expect(error).toBeInstanceOf(CommandExitError);
    const failure = error as CommandExitError;
    for (const text of [failure.message, failure.stderr, failure.stdout, failure.error ?? ""]) {
      expect(text).not.toContain(PASSWORD);
      expect(text).not.toContain(ENCODED_PASSWORD);
    }
    expect(failure.cause).toBeUndefined();
    expect(failure.exitCode).toBe(128);
  });

  test("a successful credentialed clone resets origin to the clean URL", async () => {
    const runner = new RecordingRunner();
    await git(runner).clone(ORIGIN_URL, { username: "bot", password: PASSWORD });
    expect(runner.commands).toEqual([
      `'git' 'clone' 'https://bot:${ENCODED_PASSWORD}@github.com/acme/app.git'`,
      `'git' '-C' 'app' 'remote' 'set-url' 'origin' '${ORIGIN_URL}'`,
    ]);
  });

  test("a password without username is refused before any command", async () => {
    const runner = new RecordingRunner();
    await expect(git(runner).push(REPO, { password: PASSWORD })).rejects.toBeInstanceOf(
      InvalidArgumentError,
    );
    await expect(git(runner).clone(ORIGIN_URL, { password: PASSWORD })).rejects.toBeInstanceOf(
      InvalidArgumentError,
    );
    expect(runner.runs).toHaveLength(0);
  });

  test("dangerouslyAuthenticate sets the store helper, then approves", async () => {
    const runner = new RecordingRunner();
    await git(runner).dangerouslyAuthenticate({ username: "bot", password: "tok" });
    expect(runner.commands).toEqual([
      "'git' 'config' '--global' 'credential.helper' 'store'",
      `printf %s 'protocol=https\nhost=github.com\nusername=bot\npassword=tok\n\n' | 'git' 'credential' 'approve'`,
    ]);
  });

  test("getConfig and remoteGet tolerate a missing value", async () => {
    const runner = new RecordingRunner(() => ({ stdout: "\n" }));
    expect(await git(runner).getConfig("user.name")).toBeUndefined();
    expect(await git(runner).remoteGet(REPO, "origin")).toBeUndefined();
    expect(runner.commands).toEqual([
      "'git' 'config' '--global' '--get' 'user.name' || true",
      `'git' '-C' '${REPO}' 'remote' 'get-url' 'origin' || true`,
    ]);
  });
});

describe("sandbox.git on the native Sandbox", () => {
  test("is exported and runs git through ProcessService.Start without logging the command", async () => {
    expect(ExportedGit).toBe(Git);
    expect(ExportedGitAuthError).toBe(GitAuthError);
    const { sandbox, rayd, logger } = await createTestSandbox();
    const before = logger.lines.length;
    await expect(
      sandbox.git.clone(ORIGIN_URL, { path: "/w/app", username: "bot", password: PASSWORD }),
    ).rejects.toBeInstanceOf(CommandExitError);
    const request = rayd.process.startRequests.at(-1);
    expect(request?.process?.cmd).toBe("/bin/bash");
    expect(request?.process?.args.join(" ")).toContain(
      `'git' 'clone' 'https://bot:${ENCODED_PASSWORD}@github.com/acme/app.git' '/w/app'`,
    );
    expect(request?.process?.envs.GIT_TERMINAL_PROMPT).toBe("0");
    const logged = logger.lines.slice(before);
    expect(JSON.stringify(logged)).not.toContain(PASSWORD);
    expect(JSON.stringify(logged)).not.toContain(ENCODED_PASSWORD);
    expect(logged.filter((line) => line.message.includes("git"))).toEqual([]);
  });
});
