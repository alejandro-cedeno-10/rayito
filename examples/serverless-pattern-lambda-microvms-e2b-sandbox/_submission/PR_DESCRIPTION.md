# Draft: pull request to aws-samples/serverless-patterns

Not submitted. The maintainer reviews this draft and opens the pull request
from a fork. Before opening it:

1. Copy `README.md`, `example-pattern.json`, `template.yaml` and `src/`
   (without `node_modules/`) of this folder to
   `lambda-microvms-e2b-sandbox/typescript/sam/` in a branch named
   `<github-user>-feature-lambda-microvms-e2b-sandbox` of the fork. Do not
   copy `_submission/`.
2. Add the author's LinkedIn ID and photo to `authors` in
   `example-pattern.json` if wanted, and run
   `node _scripts/validate.js lambda-microvms-e2b-sandbox/typescript/sam/example-pattern.json`
   from the repository root.
3. Run `sam validate --lint` in the pattern folder.
4. Replace the last lines of `README.md` (after `----`) with the footer of
   `_pattern-model/README.md` (`Copyright <year> Amazon.com, Inc. or its
   affiliates. All Rights Reserved.` and `SPDX-License-Identifier: MIT-0`), as
   every pattern in that repository does, and drop the
   `// SPDX-License-Identifier: MIT-0` and `# SPDX-License-Identifier: MIT-0`
   first lines of `src/*.ts` and `template.yaml` if the reviewers prefer.

---

**Title:** `New serverless pattern - lambda-microvms-e2b-sandbox`

**Body:**

*Issue #, if available:* none

*Description of changes:*

New pattern `lambda-microvms-e2b-sandbox/typescript/sam`: code sandboxes on
AWS Lambda MicroVMs, driven from TypeScript with an open-source SDK whose API
matches the E2B SDK.

- **AWS services:** AWS Lambda MicroVMs (`AWS::Lambda::MicrovmImage`), AWS
  IAM, Amazon S3 (the image artifact) and Amazon CloudWatch Logs. The optional
  part, off by default (`EnableAgent=false`), adds Amazon Bedrock and AWS
  Secrets Manager.
- **IaC:** one AWS SAM template, no custom resources and no Lambda functions.
  It creates a MicroVM image built from a signed release artifact, an IAM
  build role, log groups with 7-day retention, and least-privilege customer
  managed policies for the client (launcher, image builder, model invoke).
  Resource names are derived from the stack name; no account IDs, Regions or
  ARNs are hard-coded.
- **Client code:** four short TypeScript files in `src/`: create a sandbox,
  run Python code and shell commands, read and write files, terminate it
  (`sandbox.ts`); the same program written for the E2B SDK with only its
  import changed (`e2b-swap.ts`); and, optionally, build an agent image and
  run a coding agent that calls Amazon Bedrock through a secrets gateway, so
  code in the sandbox can use the model credential but cannot read it
  (`build-agent-image.ts`, `agent.ts`).
- **Third-party code, stated up front:** the client uses the `rayito` npm
  package (Apache-2.0), and the image is built from its release artifact
  (`rayito-image.zip`, signed with Sigstore; the README verifies it with
  cosign and SHA-256 before uploading it). Rayito is a library and an in-guest
  agent that run entirely in the customer's account; there is no external
  service, account or API key. The optional agent image downloads the pinned
  OpenCode release (MIT) during the build. Other MicroVM patterns already run
  third-party tools in the guest (Claude Code, Kiro CLI, code-server,
  OpenClaw); this pattern differs in that the client SDK is also third-party.
  If the team prefers that to live in the Serverless Land repos collection
  instead, we are happy to move it there.
- **Tested end to end** in a test account in us-east-1 on 2026-10-08,
  following the README: `sam deploy` (image built in 4 minutes), `npm run
  sandbox` and `npm run e2b` (sandbox ready in 8.5 to 10.4 seconds), the
  optional part (`EnableAgent=true` update, agent image built in 4 minutes,
  agent fixed the bug in 31 seconds with Claude Haiku 4.5), then cleanup
  (`sam delete`). All three client scripts were also run with an IAM role
  holding only the policies the README lists for them. Nothing was left
  behind. Cost of the full run: about $0.25, mostly the one-week minimum of
  image snapshot storage and Bedrock tokens.
- `example-pattern.json` passes `node _scripts/validate.js`;
  `sam validate --lint` and `cfn-lint` pass.

By submitting this pull request, I confirm that you can use, modify, copy, and redistribute this contribution, under the terms of your choice.

---

## Risks of rejection and how the draft handles them

| Risk | Evidence | Mitigation in this draft |
|---|---|---|
| Third-party SDK as the core of the pattern | PR #1581 (2023): "we don't include third-party services in patterns"; no merged pattern is built on a community SDK | Say it in the first review round; stress no external service; offer the Serverless Land repos collection as the alternative |
| Scope: "2-4 AWS services with minimal custom code" | Default path is 4 services; the optional part adds 2 | Optional part off by default; client is about 150 lines |
| Promotional content | ABCA PR #39 was closed as "an integration recipe for your specific toolchain" | Neutral wording, only AWS links in `resources`, the Rayito docs link only in the README |
| Review time | Median 34 days from open to merge across recent patterns; MicroVM PRs by AWS staff merged in 1 to 21 days; an unaffiliated MicroVM PR (#3303) has had no review since 2026-09-14 | Expect weeks; do not ping before 4 to 6 weeks |
