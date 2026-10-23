# Declarative templates

A fluent DSL (the same as E2B v2's `Template`) that compiles to a
deterministic Dockerfile and zip, composed on top of an already published
`rayito-base`/`rayito-base-caps` image, and uploads them with
`create`/`update-microvm-image`. No control plane of its own: everything happens
on the client (research §3, `docs/research/2026-10-e2b-out-of-scope.md`).
<small>Since 0.6.0.</small>

!!! info "Cost and activation"
    - **Default**: off. Without calling `Template.build()`/
      `build_in_background()` (TypeScript: `buildInBackground`), the SDK does not
      build any `lambda-microvms`/S3/Logs client: the builder
      (`Template()`, `copy()`, `pip_install()`, `dnf_install()`, `npm_install()`, `to_dockerfile()`...) is
      pure.
    - **Turns on**: `Template.build(t, "my-template", bucket="...")` /
      `Template.build(t, "my-template", { bucket: "..." })`.
    - **Resources and AWS calls**: `lambda:GetMicrovmImageVersion` (read the
      base image and, at the end, poll the build),
      `CreateMicrovmImage`/`UpdateMicrovmImage`, `GetMicrovmImage`,
      `ListMicrovmImageVersions`; `s3:GetObject` (the base image's zip),
      `HeadObject`/`PutObject` (upload the new artifact, only if it does not already
      exist by its hash); `logs:DescribeLogStreams`/`GetLogEvents` only if the
      build does not end in `SUCCESSFUL`+`ACTIVE` (to explain the failure).
      See `AWS_API_NOTES.md` §27.
    - **Approximate cost** (checked 2026-10-01): each new image version
      costs snapshot storage, ≈ $0.04/week per version
      (one-week minimum), the same as publishing with `rayito image publish`;
      the build itself is not billed separately (no CodeBuild, option A of the
      research). Each context zip stays in your bucket under
      `rayito/templates/` (standard S3 storage): put a lifecycle
      rule on that prefix; no later build reads it again.
    - **IAM**: `RayitoTemplateBuilder` (`infra/templates.yaml`) on whoever
      calls `Template.build()`:
      `UpdateMicrovmImage`/`GetMicrovmImage*`/`ListMicrovmImageVersions`
      on the account's and region's images, with an explicit `Deny`
      that prevents updating the published base images
      (`ProtectedImageNamePrefix`, `rayito-base` by default);
      `CreateMicrovmImage` on `*`, because AWS authorizes it on `*` and not
      on the new image's ARN, so it cannot be scoped by name
      (a `create` on a name that already exists fails, so it does not
      overwrite a base either); `lambda:PassNetworkConnector` on the
      AWS-managed connectors, which `create`/`update` pass even if you name
      none (`AWS_API_NOTES.md` Q114); `iam:PassRole` on the build role (the same one
      `rayito image publish` uses); `s3:PutObject`/`GetObject` on
      `rayito/templates/` of the artifact bucket and `s3:GetObject` only on
      `rayito/*` of the base image bucket (by default, the same one; never
      the whole bucket, which may also hold the persisted `HOME`s and
      the sandboxes' transfers); read access to the image's log group.
      Deliberately separate from any sandbox-launching policy.
    - **How to turn it off**: do not call `Template.build()`/
      `buildInBackground()`. The old versions of an already
      built image are deleted with `rayito image prune --image-name <name>`
      (`Template` does not delete them; `prune` keeps at least one). The
      `/rayito/<name>` log group the build role creates on the first build
      is not deleted with the image either (`delete-microvm-image` does not touch it, and
      neither the SDK nor the CLI deletes images): when retiring an image, delete it
      yourself with `aws logs delete-log-group --log-group-name /rayito/<name>`
      (`logs:DeleteLogGroup`) or set a retention on it.

!!! warning "Publish every protected name you will use first"
    Since `CreateMicrovmImage` cannot be scoped by name, whoever has
    `RayitoTemplateBuilder` can **create** an image with a protected name
    that does not yet exist in that account and region (for example a size
    or `-caps` variant you have not published). If a service later asks for
    that name (`Sandbox.create(size=...)` resolves it by suffix), it would run
    the builder's code with that service's execution role. Publish with
    `rayito image publish`, in every region you use, all the variants your
    services might ask for.

## When to use it { #cuando-usarlo }

- You are migrating an agent that already uses E2B's `Template`: the DSL is the same
  (`fromBaseImage`/`from_base_image` instead of `fromDockerImage`,
  `copy`/`pipInstall`/`setEnvs`/`workdir`/`setUser`/`setStartCmd`).
- You want your CI to build a new image from a declarative `Dockerfile`
  instead of maintaining the `rayito image publish` pipeline by
  hand.
- **When not to**: a single one-off change to an already published image —
  `rayito image publish` is still the direct path.

## Before you start: the IAM policy { #antes-de-empezar-la-politica-iam }

Whoever calls `Template.build()` needs `RayitoTemplateBuilder`, the
`templates` component of [`rayito stack`](pilas-opcionales.md). The two
required ARNs come from your IAM stack (`BuildRoleArn` is an output of
`infra/iam.yaml`):

```bash
rayito stack deploy templates \
  --param ArtifactBucketArn=arn:aws:s3:::amzn-s3-demo-bucket \
  --param BuildRoleArn=arn:aws:iam::123456789012:role/<build-role>
rayito stack status templates   # the policy's ARN, to attach it
```

`BaseImageBucketArn` is only needed if the base image's zip is in
another bucket; `ProtectedImageNamePrefix` (`rayito-base` by default) is the
prefix of the images the builder can never update.

## Differences from E2B { #divergencias-con-e2b }

- **No layer cache between builds.** Rayito has none (research
  §3.4): each `Template.build()` rebuilds everything that changed since the
  base image. A version already built with exactly the same
  artifact and the same configuration is reused without calling
  `create`/`update-microvm-image`; `skip_cache()`/`skipCache()` is equivalent to
  `force=True`/`force: true` and submits a new build anyway.
- **ARM64 only, CPU according to memory.** The native API accepts `cpu_count=`
  but ignores it; the E2B shim warns with `RayitoCompatWarning` if it is given
  (the CPU comes from `memory_mb`, Q87). `memory_mb`/`memoryMb` is validated
  against the five supported sizes (RES-1); the E2B shim rounds up to the next
  size with `RayitoCompatWarning`.
- **The composed image inherits the base's configuration.** Hooks, build
  role, managed image and `additionalOsCapabilities` come from the
  `rayito-base`/`rayito-base-caps` version you compose on: a
  template on `rayito-base-caps` still has capabilities.
- **Names without `:tag`.** A template's name is the Lambda MicroVMs image's:
  1-64 characters of `[A-Za-z0-9_-]`
  (`TemplateException`/`TemplateError` otherwise).
- **Only `fromBaseImage()` really composes.** `from_image`/`fromImage`,
  `from_template`/`fromTemplate`, `from_dockerfile`/`fromDockerfile` and
  `from_gcp_registry`/`fromGcpRegistry` raise `UnimplementedError`:
  injecting `rayd` and its hooks into an external image does not yet have a supported
  path (`rayd` only exists already installed inside a published
  `rayito-*` image). `apt_install`/`aptInstall` also raises:
  `rayito-base` is Amazon Linux 2023 (`dnf`, not `apt`): use
  `dnf_install`/`dnfInstall`.
- **No live streaming of the steps.** Each step's log is reread
  **when the build finishes** (TPL-1/Q83: the log group receives BuildKit's
  full output all at once, not live), never during.
- **E2B's tagging has no analogue.**
  `alias_exists`/`assign_tags`/`remove_tags`/`get_tags` (and their TS
  equivalents) raise `UnimplementedError`:
  `create`/`update-microvm-image` only tags the whole image, not a specific
  version.
- **`set_start_cmd`/`setStartCmd` needs a base image with `rayd`
  0.6.** The `StartSpec` is baked into `/etc/rayito/template.json`; a
  `rayd` 0.6 or later reads it on startup, launches `start_cmd` as a
  managed process before the build's `/ready`, so the snapshot already
  carries it running (it shows up in `commands.list()`), and
  answers 503 on `/ready` until `ready_cmd` exits with 0; if its deadline runs
  out, `/ready` fails and the build ends with
  `BuildException(reason="ready_server_error")`. A base image published
  with an earlier `rayd` ignores the file.
- **The `start_cmd` is already running when `/run` arrives.** Since it is in the
  snapshot, in each sandbox launched from the template it is thawed before
  the `/run` hook that installs the access token. `rayd` rejects a
  `/run` that comes from a sandbox process (any uid ≥ 1000) without
  consuming the startup's `/run`, so a malicious or compromised `start_cmd`
  cannot install its own token or leave the sandbox without
  one; only one that runs as root (`user="root"` with
  `RAYITO_ALLOW_ROOT=1` in the image) falls outside that protection
  (`SECURITY.md` T2).

## Build context { #contexto-de-build }

`copy("app/", "/srv/app/")` reads `app/` relative to `context_dir=`/
`contextDir` (by default, the current directory) and respects its
`.dockerignore`. A path that leaves the context (`../secret`, or a `src`
that is a symbolic link pointing outside) gives
`BuildException(reason="context_path_outside")`. Inside a copied directory,
symbolic links (to a file or a directory) **are never
followed**, as in Docker and in both SDKs: a `config ->
~/.aws/credentials` inside `app/` does not end up in the artifact uploaded to
S3 or in the image, where the sandbox code could read it. If
you need that content, copy it as a real file inside the context.
Files go under `__rayito_context/` inside the zip: they never replace
the composed `Dockerfile` or the base image's `rayd` binary.

### `.dockerignore` { #dockerignore }

Both SDKs follow Docker's `.dockerignore` semantics (with the
same test vectors):

- Each pattern is **anchored at the context root**: `README.md` does not
  exclude `docs/README.md`, and `/build`, `build` and `build/` are the same.
- `*` and `?` **do not cross `/`**: `*.pyc` only excludes those at the root; for
  any depth, `**/*.pyc`.
- `**` is zero or more directories: `**/.env` excludes the root's `.env` and
  those of any subdirectory (the idiom `docker init` generates).
- A pattern that matches a directory excludes everything inside it, and
  the last matching pattern wins: `node_modules` followed by
  `!node_modules/keep.js` lets `keep.js` through.

```text
**/.env
**/.env.*
**/.git
**/node_modules
**/*.pem
```

If the context is going to package something that usually carries secrets (`.env`,
`.env.*`, `.git/`, `.aws/`, `.ssh/`, `*.pem`, `*.key`), the SDK warns with
the paths (never the content) before uploading anything: in the image, any
sandbox code could read it. To make the warning stop a build in CI (the match is
against the text of the SDK's warning, which is in Spanish):

=== "Python"

    ```python
    import warnings

    from rayito import Template

    warnings.filterwarnings("error", message="el contexto de build empaqueta")
    t = Template().from_base_image().copy(".", "/app")
    Template.build(t, "my-template", bucket="my-artifact-bucket")
    ```

=== "TypeScript"

    ```ts
    import { Template } from "rayito";

    process.on("warning", (warning) => {
      if (warning.name === "RayitoContextWarning") {
        console.error(warning.message);
        process.exit(1);
      }
    });
    const t = new Template().fromBaseImage().copy(".", "/app");
    await Template.build(t, "my-template", { bucket: "my-artifact-bucket" });
    ```

!!! warning "Behavior change in 0.6.x"
    Before, `*` crossed `/` (`*.pyc` also excluded `sub/x.pyc`) and `**/X`
    did not match at the root. If your `.dockerignore` relied on the former,
    add `**/` in front of the pattern.

## Errors { #errores }

| `reason` | What happened |
|---|---|
| `None` + `step`/`command`/`exit_code`/`log_tail` | a `RUN` of the composed Dockerfile exited with an error |
| `ready_client_error` / `ready_server_error` | the process behind `/ready` answered 4xx/5xx during the build (Q85) |
| `build_quota` | 10 builds are already running in this process, or AWS rejected the account's eleventh (Q83) |
| `aws_error` | AWS rejected the build for another reason; the message and the cause carry only the sanitized code and message (without the canonical string of a signature error) |
| `build_timeout` | the build did not finish within `timeout`; it continues in AWS and `get_build_status()` queries it |
| `context_path_missing` / `context_path_outside` | a `copy()` names something that does not exist, or outside the context |
| `base_image_not_s3` / `base_image_missing_artifact` / `base_image_missing_entrypoint` | the base image is not a `rayito-*` image published with `rayito image publish` |

Messages name the template you passed, never an ARN or AWS's unsanitized
free text.

## Install packages { #instalar-paquetes }

Three shortcuts add a `RUN` step to the template's Dockerfile, like
`pip_install()`/`pipInstall()`. Names are shell-escaped and an empty
list, an empty name or one starting with `-` raises
`InvalidArgumentException`/`InvalidArgumentError` before any call
to AWS.

| Python | TypeScript | What it does |
|---|---|---|
| `dnf_install(packages)` | `dnfInstall(packages)` | `dnf install -y --setopt=install_weak_deps=0 ... && dnf clean all` (Amazon Linux 2023) |
| `npm_install(packages=None, g=False)` | `npmInstall(packages?, { g: false })` | `npm install --no-audit --no-fund [-g] [...]`; without packages it runs a plain `npm install` in the `WORKDIR` (the `package.json` copied earlier). Same signature as E2B |
| `pip_install(packages)` | `pipInstall(packages)` | `python3 -m pip install ...` |

`dnf_install` and `npm_install(g=True)` always run as root, as in E2B:
if a previous `set_user()` left another user, the step is wrapped in
`USER root` and goes back to that user afterwards. A local `npm_install` keeps
the current user and writes to the `WORKDIR`.

`rayito-base` does not bring Node.js or npm. Install them first with
`dnf_install(["nodejs20", "nodejs20-npm"])`: if npm is missing, the step fails with
code 127 and a message that explains it, instead of a cryptic
`npm: not found`.

!!! info "Measured on the base image"
    The names come from installing on the base image pinned by digest
    (`docker run`, 2026-10-14): `nodejs20` brings Node 20.20.2 and `nodejs20-npm`
    npm 10.8.2, with `node` and `npm` on the `PATH`. The bare `nodejs` package
    is Node 18, out of support. The `dnf` flag is
    `install_weak_deps=0`: this `dnf` rejects `False`.

=== "Python"

    ```python
    from rayito import Template

    t = (
        Template()
        .from_base_image("rayito-base")
        .dnf_install(["tree", "nodejs20", "nodejs20-npm"])
        .npm_install(["left-pad@1.3.0"])
        .npm_install("typescript", g=True)
    )
    ```

=== "TypeScript"

    ```ts
    import { Template } from "rayito";

    const t = new Template()
      .fromBaseImage("rayito-base")
      .dnfInstall(["tree", "nodejs20", "nodejs20-npm"])
      .npmInstall(["left-pad@1.3.0"])
      .npmInstall("typescript", { g: true });
    ```

`apt_install`/`aptInstall` still raises `UnimplementedError`, and its message
points to `dnf_install`/`dnfInstall`.

## Quick example { #ejemplo-rapido }

=== "Python"

    ```python
    from rayito import Sandbox, Template, wait_for_port

    t = (
        Template()
        .from_base_image("rayito-base")  # (1)!
        .pip_install(["pandas==2.2.3"])
        .copy("app/", "/srv/app/")
        .set_envs({"MODE": "prod"})
        .set_start_cmd("python3 -m http.server 8000", wait_for_port(8000))
    )
    info = Template.build(t, "my-template", bucket="my-artifact-bucket")  # (2)!
    print(info.template_id)  # arn:aws:lambda:...:microvm-image:my-template

    with Sandbox.create(info.template_id) as sbx:
        sbx.commands.run("curl -s localhost:8000")
    ```

    1. Pure: it does not call AWS until `Template.build()`.
    2. Resolves the latest `ACTIVE` version of `rayito-base`, composes the
       Dockerfile, uploads the zip and waits for the build to finish.

=== "Python (async)"

    ```python
    import asyncio

    from rayito import AsyncTemplate, Template, wait_for_port


    async def main() -> None:
        t = Template().from_base_image().set_start_cmd(
            "python3 -m http.server 8000", wait_for_port(8000)
        )
        info = await AsyncTemplate.build(t, "my-template", bucket="my-artifact-bucket")
        print(info.template_id)


    asyncio.run(main())
    ```

=== "TypeScript"

    ```ts
    import { Sandbox, Template, waitForPort } from "rayito";

    const t = new Template()
      .fromBaseImage("rayito-base")
      .pipInstall(["pandas==2.2.3"])
      .copy("app/", "/srv/app/")
      .setEnvs({ MODE: "prod" })
      .setStartCmd("python3 -m http.server 8000", waitForPort(8000));

    const info = await Template.build(t, "my-template", { bucket: "my-artifact-bucket" });
    console.log(info.templateId);

    await using sbx = await Sandbox.create({ template: info.templateId });
    ```

=== "Shim E2B"

    ```python
    from rayito.e2b import E2B, Template

    client = E2B(region="us-east-1", bucket="my-artifact-bucket")  # (1)!
    t = Template().from_base_image().pip_install(["pandas==2.2.3"])
    info = client.Template.build(t, alias="my-template", memory_mb=2048, skip_cache=False)
    ```

    1. `bucket=` is a Rayito extension; it is also valid on each call.

    In TypeScript, with E2B JS's options shape:

    ```ts
    import { E2B, Template } from "rayito/e2b";

    const client = new E2B({ region: "us-east-1", bucket: "my-artifact-bucket" });
    const t = new Template().fromBaseImage().pipInstall(["pandas==2.2.3"]);
    const info = await client.Template.build(t, { alias: "my-template", memoryMB: 2048 });
    ```

=== "CLI"

    ```bash
    rayito template build my_template.py --name my-template --bucket my-artifact-bucket
    rayito template status my-template
    rayito template logs my-template
    ```

## Template environment variables { #variables-del-template }

The variables of `set_envs()`/`setEnvs()` are visible to the sandbox's
commands, terminals and `run_code`, as in E2B: `Template.build()` writes them
to `/etc/rayito/template-env.json` and `rayd` reads them at boot. A variable
of `Sandbox.create(envs=...)` wins over the template's one with the same
name, and a command's variable wins over both. They are still in the
Dockerfile `ENV`, so the build's own `RUN` steps see them.

This needs a base image published with the `rayd` of 0.17.1 or later: an
older `rayd` ignores the file and only the build's `RUN` steps see the
variables. A template that already used `set_envs()` creates a new version on
its next `build()`, because the artifact carries that file.

!!! warning "Do not store secrets with `set_envs()`"
    The variables stay in the image and in every snapshot. For a secret use
    [Secrets](../secrets.md).

## `Template.build()` options { #opciones-de-templatebuild }

| Python | TypeScript | Default | What it does |
|---|---|---|---|
| `bucket` | `bucket` | — (required) | artifact bucket where the zip is uploaded, under `rayito/templates/` |
| `memory_mb` | `memoryMb` | `2048` | image size, one of 512/1024/2048/4096/8192 |
| `force` | `force` | `False` | builds even if a version with the same artifact already exists (`skip_cache()`) |
| `timeout` (s) | `timeoutMs` | 1800 s | maximum wait for the build (`build()` only); when it runs out, `BuildException(reason="build_timeout")` |
| `base_image_version` | `baseImageVersion` | the latest `ACTIVE` | version of the base image you compose on |
| `on_build_logs` | `onBuildLogs` | — | receives the build log line by line, at the end (`build()` only) |
| `context_dir` | `contextDir` | the current directory | root of `copy()` paths |
| `region`, `session` | `region`, `credentials` | the default session | where and with which credentials |
| `cpu_count`, `build_role_arn` | — | — | accepted and ignored: the CPU comes from `memory_mb` and the build role is the base image's |

`Template.exists(name)` tells you whether the image already exists, and
`t.to_dockerfile()`/`t.to_json()` (`toDockerfile()`/`toJSON()`) show what
is going to be built without calling AWS. With the CLI, `rayito template build`
reads a `.py` file that defines a variable `template`.

## A build that fails { #un-build-que-falla }

=== "Python"

    ```python
    from rayito import BuildException, Template

    t = Template().from_base_image().pip_install(["this-package-does-not-exist"])
    try:
        Template.build(t, "my-broken-template", bucket="my-artifact-bucket")
    except BuildException as exc:
        print(exc.step, exc.command, exc.exit_code)  # 1 'RUN pip install ...' 1
    ```

=== "TypeScript"

    ```ts
    import { BuildError, Template } from "rayito";

    const t = new Template().fromBaseImage().pipInstall(["this-package-does-not-exist"]);
    try {
      await Template.build(t, "my-broken-template", { bucket: "my-artifact-bucket" });
    } catch (error) {
      if (error instanceof BuildError) {
        console.log(error.step, error.command, error.exitCode); // 1 'RUN pip install ...' 1
      }
    }
    ```

## Background build and logs { #build-en-segundo-plano-y-logs }

`Template.build()` waits for the build to finish (at most `timeout`, 30
minutes by default). To avoid blocking, `build_in_background()`
(TypeScript: `buildInBackground()`) returns as soon as AWS accepts the build
a `BuildHandle`, which `get_build_status()` queries whenever you want, also
from another process:

=== "Python"

    ```python
    import time

    from rayito import Template

    t = Template().from_base_image().pip_install(["pandas==2.2.3"])
    handle = Template.build_in_background(t, "my-template", bucket="my-artifact-bucket")
    status = Template.get_build_status(handle)
    while status.state == "IN_PROGRESS":
        time.sleep(15)
        status = Template.get_build_status(handle)
    if status.info is not None:
        print(status.info.template_id)  # SUCCESSFUL
    else:
        print(status.error_message)  # FAILED
    ```

=== "TypeScript"

    ```ts
    import { setTimeout as sleep } from "node:timers/promises";
    import { Template } from "rayito";

    const t = new Template().fromBaseImage().pipInstall(["pandas==2.2.3"]);
    const handle = await Template.buildInBackground(t, "my-template", {
      bucket: "my-artifact-bucket",
    });
    let status = await Template.getBuildStatus(handle);
    while (status.state === "IN_PROGRESS") {
      await sleep(15_000);
      status = await Template.getBuildStatus(handle);
    }
    console.log(status.info?.templateId ?? status.errorMessage);
    ```

=== "CLI"

    ```bash
    rayito template status my-template            # the most recent version
    rayito template status my-template --version 3
    rayito template logs my-template --limit 200
    ```

BuildKit's log arrives **when the build finishes**, never live (the image's
log group receives it all at once, Q83). To receive it line by line
from `Template.build()`, pass `on_build_logs` (TypeScript:
`onBuildLogs`); without it, the SDK does not read the log group except to explain
a failure:

=== "Python"

    ```python
    from rayito import Template

    t = Template().from_base_image().pip_install(["pandas==2.2.3"])
    Template.build(t, "my-template", bucket="my-artifact-bucket", on_build_logs=print)
    ```

=== "TypeScript"

    ```ts
    import { Template } from "rayito";

    const t = new Template().fromBaseImage().pipInstall(["pandas==2.2.3"]);
    await Template.build(t, "my-template", {
      bucket: "my-artifact-bucket",
      onBuildLogs: (line) => console.log(line),
    });
    ```

In TypeScript, `onBuildLogs` and the explanation of a failed build read the
log with the optional peer `@aws-sdk/client-cloudwatch-logs`
(`pnpm add @aws-sdk/client-cloudwatch-logs`); without it installed, the build
works the same, but `onBuildLogs` receives no lines and
`BuildError` arrives without `step`/`command`/`logTail`.

## From E2B's `Template` to Rayito { #del-template-de-e2b-a-rayito }

The shim (`rayito.e2b` / `rayito/e2b`) accepts E2B's build signature and
translates it to the native one; the DSL is the same class in both.

| E2B | Native Rayito | Note |
|---|---|---|
| `Template().from_image("python:3.12")`, `from_template`, `from_dockerfile`, `from_gcp_registry` | `Template().from_base_image("rayito-base")` | it only composes on a published `rayito-*` image; those four sources raise `UnimplementedError` |
| `copy`, `run_cmd`, `pip_install`, `npm_install(packages=None, g=False)`, `set_envs`, `set_user`, `skip_cache` | the same methods | `skip_cache()` is equivalent to `force=True`; `npm_install` needs Node.js in the image (see above) |
| `set_workdir(path)` | `workdir(path)` | — |
| `apt_install([...])` | `dnf_install([...])` | `rayito-base` is Amazon Linux 2023; `apt_install` raises `UnimplementedError` and points to `dnf_install` |
| `set_start_cmd(cmd, wait_for_port(8000))` | the same, also `wait_for_url`/`wait_for_process`/`wait_for_file` | needs a base image with `rayd` 0.6 |
| `Template.build(t, alias="x", cpu_count=, memory_mb=, skip_cache=, on_build_logs=)` | `Template.build(t, "x", bucket=, memory_mb=, force=, on_build_logs=)` | the shim asks for `bucket` in the call or in `E2B(bucket=...)`; `memory_mb` is rounded up to the next size and `cpu_count` is ignored, both with `RayitoCompatWarning` |
| `Template.build_in_background(...)` + `Template.get_build_status(...)` | the same, with a `BuildHandle` | — |
| `Template.exists(name)` | the same | — |
| `alias_exists`, `assign_tags`, `remove_tags`, `get_tags` | — | raise `UnimplementedError`: Lambda MicroVMs tags the whole image, not a version |
| `TemplateException`, `BuildException` | the same classes | `BuildException` carries `reason`, `step`, `command`, `exit_code` and `log_tail` |

## Reference { #referencia }

- [`rayito.Template`/`rayito.AsyncTemplate`](../referencia/python/opcionales.md#templates-declarativos)
- `AWS_API_NOTES.md` §27 (build logs and the `codeArtifact` contract)
- `ARCHITECTURE.md` ADR-022
