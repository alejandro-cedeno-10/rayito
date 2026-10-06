## Context

`check_hygiene.py` is a CI gate over `git ls-files`: standard library, no
network, line by line, `KO <path>:<line>: <rule>` without the line. Its
rules cover account IDs in ARNs and bucket names, the CDK bootstrap
qualifier, SSO profiles and roles, MicroVM IDs, four network prefixes,
local paths and access keys. The 2026-10 leak audit found nothing to scrub
but listed what the gate could not see.

## Goals / Non-Goals

**Goals:** catch every fixed-shape identifier an AWS acceptance run can
leave behind; catch the organization's name and accounts without writing
them anywhere public; give PR text and the history the same scrutiny as
the tree; keep every finding free of the value it found.

**Non-Goals:** rewriting history (a maintainer decision, with a GitHub
support purge of cached views); scanning issue comments or release notes
in CI (they have no event with the text before it is public); semantic
detection of names that have no fixed shape and are not on the list.

## Decisions

### D1. The organization's terms live outside the repository

Listing them in the gate, in a test or even as a hash would publish them
(a short name falls to a dictionary attack on any hash). They come from
`RAYITO_HYGIENE_DENYLIST` (comma- or newline-separated, a repository
secret, so GitHub masks it in logs and forks never see it) and from
`$(git rev-parse --git-path info/hygiene-denylist)`, which Git never
tracks or pushes and which works from worktrees. Matching is a
case-folded substring, so a term also catches its domains and URLs. A
finding says "término de la lista privada" and nothing else. Without
either source the rule is inert; the fixed-shape rules still run.

### D2. Low-entropy suffixes are placeholders

Real AWS resource IDs are random hex. Tests already use several distinct
fakes per prefix (`fs-0123abcd`, `fsap-0456abcd`, `fs-99999999`), so a
single allowed value cannot work. A suffix that contains `0123` or
`abcd`, or repeats a character six times in a row, passes. The chance
that a random 8-hex ID matches is about 1 in 6,500, and a 17-hex one
about 1 in 2,300: accepted for a hygiene gate whose real defence is reviewing the
diff. `nat-`, `lt-` and `i-` take only the 17-hex form, because their
8-hex form collides with ordinary words.

### D3. Account keys and repeated digits

An account ID outside an ARN is only recognisable by its key, so the new
rule fires on 12 digits after `account`, `account_id`, `accountId`,
`sso_account_id`, `"Account":` or `--account-id`. Accounts made of one
repeated digit join the documented placeholders in every account rule,
which the template tests already use as fake accounts.

### D4. gitleaks over the history, pinned and redacted

The gate sees the tree; a leak removed in a later commit still lives in
the history. `gitleaks git --log-opts=HEAD --redact` with the default
rules scans every commit reachable from the checked-out ref (main plus
the PR on a pull request) in about three seconds. The binary is
downloaded from the release, verified against its sha256 and run in a
`contents: read` job with no cache and no persisted credentials. The 39
false positives found in the existing history (test placeholders, the TLS
test key of `rayd`'s fixtures) are listed by fingerprint in
`.gitleaksignore`, so any new finding fails.

### D5. PR text through the same gate

`check_hygiene.py -` reads stdin and reports it as `<stdin>`. The
`pr-text` job feeds it the title and body through environment variables
(never interpolated into the script) on `opened`, `edited`, `reopened`
and `synchronize`, with the denylist secret. A failure cannot unpublish
the text, but it flags it while the author can still edit it.

## Risks / Trade-offs

- A denylist term that is an ordinary word fails unrelated lines; the
  maintainer chooses the terms and the finding names the line.
- The PR-text job runs after the text is public; it shortens the window,
  it does not close it. The skill asks agents to run the gate on the text
  before posting.
