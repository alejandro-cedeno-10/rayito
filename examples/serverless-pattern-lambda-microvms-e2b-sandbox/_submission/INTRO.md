# Intro text (300-500 words)

Draft of the Serverless Land intro for the pattern. It is the same text as `introBox.text` in `../example-pattern.json` (369 words); keep both in sync.

## How it works

This pattern gives you code sandboxes, isolated environments where an application or an AI agent runs untrusted code, on AWS Lambda MicroVMs in your own AWS account. Each sandbox is one MicroVM with its own kernel, memory and disk. You drive it from TypeScript or Python with Rayito, an open-source (Apache-2.0) SDK whose API matches the E2B SDK, so code written for E2B runs after changing one import line. There is no third-party service and no API key: the SDK calls the Lambda MicroVMs APIs with your AWS credentials.

The AWS SAM template builds an AWS::Lambda::MicrovmImage on the AWS managed Amazon Linux 2023 base image from the signed release artifact of Rayito, which you download, verify and upload to Amazon S3. The artifact contains a small agent that serves the MicroVM lifecycle hooks and an API for processes, files and a stateful Python kernel. Lambda boots the image, waits for the ready hook, validates it and takes a snapshot, so every sandbox starts from a warm snapshot in seconds. The template also creates an IAM build role, an Amazon CloudWatch Logs log group and a least-privilege customer managed policy that lets the client run, drive and terminate MicroVMs of this stack's images but not change them.

A short TypeScript client creates a sandbox (RunMicrovm), runs Python code whose variables persist between calls, runs shell commands, writes and reads files, and terminates the MicroVM (TerminateMicrovm). A second client is the same program written for the E2B SDK with only its import changed.

An optional part, off by default, builds a second image with additional OS capabilities and composes on it an image with the OpenCode coding agent. The client mints a short-term Amazon Bedrock API key, stores it in AWS Secrets Manager, and starts a sandbox without Internet access whose only way out is a gateway in the sandbox agent: it adds the key to requests for one allowed model, so code in the sandbox can use the key but cannot read it. The agent then fixes a bug in a file inside the MicroVM.

This pattern deploys one or two MicroVM images, one IAM role, up to three CloudWatch Logs log groups and up to three customer managed policies.
