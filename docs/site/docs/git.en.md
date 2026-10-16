# Git

`sbx.git` is the E2B SDK's git API on top of `commands.run`: each method
builds the `git` command with E2B's arguments and runs it inside the
sandbox as the sandbox user. `rayito-base` ships `git-core`
(`git-core-2.50.1-1.amzn2023.0.1`, pinned in `image/Dockerfile`).

!!! note "E2B marks this module as deprecated"
    Rayito offers it for parity. Nothing stops you from using
    `sbx.commands.run("git ...")` directly.

=== "Python"

    ```python
    from rayito import Sandbox

    with Sandbox.create() as sbx:
        sbx.git.clone("https://github.com/octo/demo.git", path="/home/user/demo", depth=1)
        sbx.git.configure_user("Agent", "agent@example.com")
        sbx.files.write("/home/user/demo/NOTES.md", "hello\n")
        sbx.git.add("/home/user/demo")
        sbx.git.commit("/home/user/demo", "notes")
        status = sbx.git.status("/home/user/demo")
        print(status.current_branch, status.ahead, status.is_clean)
        print(sbx.git.branches("/home/user/demo").branches)
    ```

=== "Python (async)"

    ```python
    import asyncio
    import os

    from rayito import AsyncSandbox


    async def main() -> None:
        async with await AsyncSandbox.create() as sbx:
            repo = "/home/user/demo"
            await sbx.git.clone("https://github.com/octo/demo.git", path=repo, depth=1)
            await sbx.git.create_branch(repo, "changes")
            await sbx.files.write(f"{repo}/NOTES.md", "hello\n")
            await sbx.git.add(repo)
            await sbx.git.commit(repo, "notes", author_name="Agent", author_email="agent@example.com")
            # short-lived, single-repo token: the sandbox code can see it
            await sbx.git.push(repo, username="x-access-token", password=os.environ["GIT_TOKEN"])
            print((await sbx.git.status(repo)).ahead)


    asyncio.run(main())
    ```

=== "TypeScript"

    ```ts
    import { Sandbox } from "rayito";

    await using sbx = await Sandbox.create();
    const repo = "/home/user/demo";
    await sbx.git.clone("https://github.com/octo/demo.git", { path: repo, depth: 1 });
    await sbx.git.configureUser("Agent", "agent@example.com");
    await sbx.git.createBranch(repo, "changes");
    await sbx.files.write(`${repo}/NOTES.md`, "hello\n");
    await sbx.git.add(repo);
    await sbx.git.commit(repo, "notes");
    const status = await sbx.git.status(repo);
    console.log(status.currentBranch, status.isClean, status.ahead);
    await sbx.git.push(repo, { username: "x-access-token", password: process.env.GIT_TOKEN ?? "" });
    console.log((await sbx.git.branches(repo)).branches);
    ```

Methods (the same in `Git` and `AsyncGit`; in TypeScript in camelCase):
`clone`, `init`, `remote_add`, `remote_get`, `status`, `branches`,
`create_branch`, `checkout_branch`, `delete_branch`, `add`, `commit`,
`reset`, `restore`, `push`, `pull`, `set_config`, `get_config`,
`dangerously_authenticate` and `configure_user`. All of them also accept
`envs`, `user`, `cwd`, `timeout` and `request_timeout`, and set
`GIT_TERMINAL_PROMPT=0` (a git that would ask for a password fails instead
of hanging). In TypeScript the required arguments remain positional
(`path`, `branch`, `message`, `url` in `clone`…) and the optional ones go in
an object (`{ path, depth }`, `{ username, password }`,
`{ authorName, authorEmail }`…); `dangerouslyAuthenticate({ username,
password, host })` takes everything in the object.

`status()` returns a `GitStatus` (`current_branch`, `upstream`, `ahead`,
`behind`, `detached`, `file_status` and the properties `is_clean`,
`has_changes`, `has_staged`, `has_untracked`); `branches()`, a
`GitBranches` (`branches`, `current_branch`); `get_config()` and
`remote_get()`, the value or `None`. The rest return the command's
`CommandResult`.

## Credentials { #credenciales }

!!! warning "Treat the token as disclosed to the sandbox code"
    A `username`/`password` you pass to `clone`, `push` or `pull` is within
    reach of any code that has already run in that sandbox with the same
    user: it may have prepared its `~/.bash_profile` (commands run with a
    login shell), its git configuration or a process that watches `/proc`.
    Use only short-lived, single-repository tokens with the minimum
    permission (for example, a GitHub App installation token), never a
    long-lived personal token in a sandbox that runs untrusted code. It is
    the same rule as for [injected secrets](secrets.md).

- **`clone`, `push` and `pull` with `username`/`password`**: the URL with
  credentials is in `.git/config` while the command lasts. After a `clone`
  the SDK leaves the `origin` remote with the URL without credentials
  (unless `dangerously_store_credentials=True`); in `push`/`pull` it
  changes the remote's URL, runs the command and always restores it, even
  if the command fails or expires. If the restore cannot be done (the
  sandbox is paused, the stream is cut), the `rayito.git` logger (in
  TypeScript, the sandbox's `logger`) warns, without the URL, that the
  token may still be in `.git/config`, and would travel in a snapshot or in
  `persist=`. A `password` without `username` raises
  `InvalidArgumentException` before running anything.
- **Hardening**: with credentials, the git command runs without hooks
  (`-c core.hooksPath=/dev/null`) and without credential helpers
  (`-c credential.helper=`, so a configured helper does not store the token
  on disk), and before sending anything the SDK checks that the git
  configuration does not rewrite URLs (`url.*.insteadOf`); if it does, it
  raises `GitAuthException` without having sent the credentials.
- **The credentials travel in the command** (`StartRequest.cmd`) to
  `rayd`, which never logs commands; the SDK does not log them either and
  redacts them (`***`) from `stdout`, `stderr` and the message of a
  `CommandExitException`.
- **`dangerously_authenticate(username, password, host="github.com")`**
  stores the credentials with `git credential approve` in the sandbox
  user's `~/.git-credentials`: **they are visible to the sandbox code**,
  just as in E2B. Use it only with a minimal-scope, short-lived token
  ([Security](security.md)).

## Errors { #errores }

| Situation | Python | TypeScript |
|---|---|---|
| the remote asks for credentials | `GitAuthException` (never includes the URL) | `GitAuthError` |
| the git configuration rewrites URLs (`url.*.insteadOf`) and the call carries credentials | `GitAuthException`, without sending them | `GitAuthError` |
| `push`/`pull` without a tracking branch | `GitUpstreamException` with the missing command | `GitUpstreamError` |
| any other git failure | `CommandExitException` (output redacted if there were credentials) | `CommandExitError` |
| invalid arguments | `InvalidArgumentException`, before running anything | `InvalidArgumentError` |

In the E2B shim, `sbx.git` is the same object and the exceptions have the
names from `e2b.exceptions` (`GitAuthException`, `GitUpstreamException`).
