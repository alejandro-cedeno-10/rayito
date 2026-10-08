# Outreach drafts

Nothing here has been posted or sent. The maintainer reviews, edits and
publishes each draft.

## Read first: the state of issue #645

[aws-samples/sample-autonomous-cloud-coding-agents#645](https://github.com/aws-samples/sample-autonomous-cloud-coding-agents/issues/645)
(Lambda MicroVMs as a `ComputeStrategy` backend) is not an open request
waiting for an implementation. Checked on 2026-10-08:

- It is `approved`, assigned to an AWS maintainer, and scope-frozen under the
  repository's governance (ADR-003: no branch or PR without an approved,
  assigned issue; new dependencies are "ask first" in `AGENTS.md`).
- The backend already exists on `main`, built directly on
  `@aws-sdk/client-lambda-microvms`: ADR-021 (#688), P1 (#689: strategy, CDK
  construct, bootstrap IAM) and P2 (#733: live clone → change → PR smoke test)
  are merged. P3 (mandatory `suspendSession`/`resumeSession` across all three
  strategies, approval-wait suspend policy) has been handed to another AWS
  maintainer.
- ADR-021 requires an explicit `NO_INGRESS` connector on every MicroVM. The
  Rayito SDK reaches its in-guest agent through the MicroVM endpoint, so a
  Rayito-based strategy would need ingress: a security delta against their
  decision.
- A precedent: PR #39 in that repository, a guide for the author's own
  toolchain, was closed as "not a neutral documentation but an integration
  recipe for your specific toolchain", with a request to open an RFC instead.

So a second, Rayito-based `lambda-microvm` strategy would duplicate merged
work, add a third-party dependency and weaken a security decision; it would
very likely be declined. The drafts below therefore do **not** announce an
implementation. (a) offers measurements that touch P3's open items, which is
useful to them and costs nothing; (b) is an RFC-first path in case the
maintainers say they want an E2B-compatible option, and a PR description to
use only after they approve it.

## (a) Comment for issue #645

> Hi @dreamorosi, @isadeks, thanks for writing the P1/P2 findings up in so
> much detail; several match what we measured independently while building
> [Rayito](https://alejandro-cedeno-10.github.io/rayito/), an open-source
> (Apache-2.0) E2B-compatible sandbox SDK on Lambda MicroVMs. In case it
> helps P3, here are a few live measurements (us-east-1, 2026-09/10) that
> touch the suspend/resume work. The raw notes are in
> [`AWS_API_NOTES.md`](https://github.com/alejandro-cedeno-10/rayito/blob/main/AWS_API_NOTES.md)
> (in Spanish; the Q numbers below are its row IDs):
>
> - **Suspend and resume are idempotent** (Q38): `SuspendMicrovm` on an
>   already `SUSPENDED` VM and `ResumeMicrovm` on a `RUNNING` one both return
>   200, not the documented `ConflictException`. That makes the inline,
>   best-effort resume in approve/deny safe to retry.
> - **`GetMicrovm` lags behind a resume**: 0.1 to 0.5 s after the `/resume`
>   hook had returned 200 and the guest was serving, `GetMicrovm` still said
>   `PENDING`. A poll right after a resume can see a transitional state.
> - **A `/suspend` hook that returns 500 terminates the VM** within 5 s, with
>   `stateReason` "Suspend lifecycle hook returned HTTP status 500…" and no
>   retry. The hook has to answer 200 even when its own flush fails.
> - **Long suspends**: a VM suspended for 70 min (4,200 s) resumed in about
>   1 s, and IMDS served fresh execution-role credentials after the original
>   ones had expired (Q81, Q139). That is one more data point inside your open
>   1 h to 8 h window, not an answer to it.
> - **Suspend and resume latency** at 2 GB: `SUSPENDED` 1.4 s after the call;
>   first 200 from the guest 1.2 s after `ResumeMicrovm`.
> - **One egress connector per VM** (Q131): passing `INTERNET_EGRESS` plus a
>   VPC connector is a `ValidationException` "Only one egress network
>   connector can be provided".
> - **CloudFormation-managed images work for us**: two
>   `AWS::Lambda::MicrovmImage` resources (all six hooks `ENABLED`, one with
>   `AdditionalOsCapabilities: [ALL]`) created and deleted cleanly from a SAM
>   template on 2026-10-08, in case the CDK path is revisited. We did not
>   test an in-place image update through CloudFormation.
>
> We are not proposing another backend: the native strategy is the right
> place for this. If an E2B-compatible SDK option for the sandbox side would
> ever be useful to ABCA, I'm happy to write it up as a separate RFC first.
> Happy to re-run any of these measurements on request.

## (b) Only if a maintainer asks for it

### RFC issue (to open first, never a PR without it)

> **Title:** RFC: optional E2B-compatible sandbox access for agents on the
> `lambda-microvm` backend
>
> **Component:** agent runtime, `lambda-microvm` backend
>
> **Describe the feature:** let an agent running on ABCA create short-lived
> child sandboxes (one Lambda MicroVM each) through an E2B-compatible API, to
> run untrusted code or tests outside its own session VM, using the
> open-source Rayito SDK in the customer's account. The session backend
> stays the native `LambdaMicrovmComputeStrategy`; nothing in the
> orchestrator changes.
>
> **Non-goals:** replacing or wrapping `LambdaMicrovmComputeStrategy`;
> changing the `ComputeStrategy` interface; adding ingress to session VMs.
>
> **Open questions for the maintainers:** whether a third-party dependency
> is acceptable in `agent/` at all; whether child sandboxes need ingress
> (the SDK uses the MicroVM endpoint) and how that fits ADR-021's
> `NO_INGRESS`; where the image build lives (CDK vs operator step).

### PR description (after the RFC is approved and assigned)

> **Title:** `feat(agent): optional E2B-compatible child sandboxes on Lambda MicroVMs`
>
> **Body:**
>
> Closes #<rfc-issue>.
>
> Motivation: <one paragraph from the approved RFC>.
>
> What changes:
> - `agent/`: an optional tool that creates a child sandbox with the
>   `rayito` SDK, runs a command or code in it and terminates it; off unless
>   the Blueprint enables it.
> - `cdk/`: an optional construct for the sandbox image
>   (`AWS::Lambda::MicrovmImage`) and a launcher policy scoped to that image.
> - Docs: `COMPUTE.md` note and a guide section.
>
> Tests: unit tests for the tool (mocked SDK), CDK assertions for the
> optional construct, `mise run build` green. Live run: <date, Region, cost>.
>
> Security deltas: <ingress decision from the RFC>; the model credential is
> never passed to the child sandbox.
>
> Checklist: unit test added; integration test (new CloudFormation resource
> type); docs updated; conventional commit title.

## (c) Message to the Lambda MicroVMs / Serverless Developer Advocacy team

> **Subject:** Community sample: E2B-compatible sandboxes on Lambda MicroVMs
>
> Hi <name>,
>
> I maintain Rayito, an open-source (Apache-2.0) SDK for Python and
> TypeScript that gives Lambda MicroVMs the same API as the E2B sandbox SDK:
> teams move existing E2B code into their own AWS account by changing one
> import line, with no external service. It covers commands, files, a
> stateful Python kernel, pause/resume, persistence beyond 8 hours, closed
> egress, and an in-sandbox coding agent whose model credential stays behind
> a secrets gateway. Docs: https://alejandro-cedeno-10.github.io/rayito/ ;
> code: https://github.com/alejandro-cedeno-10/rayito ; npm and PyPI:
> `rayito` 0.10.0.
>
> I've prepared a Serverless Land pattern (SAM, `AWS::Lambda::MicrovmImage`,
> least-privilege IAM, tested end to end) and would like to know which home
> you'd prefer before I open anything:
>
> 1. the serverless-patterns pull request as drafted, given that its client
>    is a community SDK;
> 2. the Serverless Land repos collection, with a fuller sample (for example
>    migrating an E2B code-interpreter app, or an agent with closed egress);
> 3. a guest post for the AWS Compute Blog or community.aws on what we
>    measured running sandboxes on Lambda MicroVMs (cold start and resume
>    latency, hook behaviour, IAM and cost), which I'm happy to draft.
>
> Thanks for your time,
> Alejandro
