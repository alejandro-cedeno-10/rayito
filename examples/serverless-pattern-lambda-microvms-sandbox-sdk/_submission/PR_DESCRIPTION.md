# Pull request to aws-samples/serverless-patterns

Submitted on 2026-10-08 as
[aws-samples/serverless-patterns#3336](https://github.com/aws-samples/serverless-patterns/pull/3336),
from the branch `alejandro-cedeno-10-feature-lambda-microvms-sandbox-sdk` of
the fork `alejandro-cedeno-10/serverless-patterns`. The pattern folder upstream
is `lambda-microvms-sandbox-sdk/typescript/sam`, following the
`{family}/{language}/{framework}` layout of their `PUBLISHING.md`.

The submission differs from the first draft of this folder: it drops every
mention of E2B and the `src/e2b-swap.ts` client (`npm run e2b`), and the
README ends with the Amazon copyright footer of `_pattern-model`. The rest of
this folder matches what was submitted. `package-lock.json` is force-added
upstream because their `.gitignore` ignores lockfiles and the README uses
`npm ci`.

Checks run before opening it: `node _scripts/validate.js` on
`example-pattern.json`, `sam validate --lint`, `cfn-lint`, `npm ci` and
`tsc --noEmit` in `src/`, and `scripts/check_hygiene.py` on the pattern files
and the PR body.

---

**Title:** `New serverless pattern - lambda-microvms-sandbox-sdk`

**Body:**

*Issue #, if available:* none

*Description of changes:*

New pattern `lambda-microvms-sandbox-sdk/typescript/sam`: spin up isolated code sandboxes on AWS Lambda MicroVMs in seconds, from a few lines of TypeScript, using an open-source sandbox SDK.

- **AWS services:** AWS Lambda MicroVMs (`AWS::Lambda::MicrovmImage`), AWS IAM, Amazon S3 (the image artifact) and Amazon CloudWatch Logs. The optional part, off by default (`EnableAgent=false`), adds Amazon Bedrock and AWS Secrets Manager.
- **IaC:** one AWS SAM template, no custom resources and no Lambda functions. It creates a MicroVM image built from a signed release artifact, an IAM build role, log groups with 7-day retention, and least-privilege customer managed policies for the client (launcher, image builder, model invoke). Resource names are derived from the stack name; no account IDs, Regions or ARNs are hard-coded.
- **Client code:** three short TypeScript files in `src/`: create a sandbox, run Python code and shell commands, read and write files, terminate it (`sandbox.ts`); and, optionally, build an agent image and run a coding agent that calls Amazon Bedrock through a secrets gateway, so code in the sandbox can use the model credential but cannot read it (`build-agent-image.ts`, `agent.ts`). The same SDK is also published for Python (`pip install rayito`), with matching sync and async APIs, so the pattern can be followed from either language.
- **What runs inside each MicroVM:** `rayd`, a small static agent written in Rust, packaged in the signed image artifact. It serves the MicroVM lifecycle hooks (run, ready, suspend, resume, terminate) and a gRPC API for processes, files, PTY and a stateful Python kernel. It is the only thing the client talks to inside the VM.
- **Third-party code, stated up front:** the client uses the `rayito` npm package (Apache-2.0), and the image is built from its release artifact (`rayito-image.zip`, signed with Sigstore; the README verifies it with cosign and SHA-256 before uploading it). Rayito is a library and an in-guest agent that run entirely in the customer's account; there is no external service, account or API key. The optional agent image downloads the pinned OpenCode release (MIT) during the build. Other MicroVM patterns already run third-party tools in the guest (Claude Code, Kiro CLI, code-server, OpenClaw); this pattern differs in that the client SDK is also third-party. If the team prefers that to live in the Serverless Land repos collection instead, we are happy to move it there.
- **Tested end to end** in a test account in us-east-1 on 2026-10-08, following the README: `sam deploy` (image built in 4 minutes), `npm run sandbox` (sandbox ready in 8.5 to 10.4 seconds), the optional part (`EnableAgent=true` update, agent image built in 4 minutes, agent fixed the bug in 31 seconds with Claude Haiku 4.5), then cleanup (`sam delete`). The client scripts were also run with an IAM role holding only the policies the README lists for them. Nothing was left behind. Cost of the full run: about $0.25, mostly the one-week minimum of image snapshot storage and Bedrock tokens.
- `example-pattern.json` passes `node _scripts/validate.js`; `sam validate --lint` and `cfn-lint` pass.

By submitting this pull request, I confirm that you can use, modify, copy, and redistribute this contribution, under the terms of your choice.
