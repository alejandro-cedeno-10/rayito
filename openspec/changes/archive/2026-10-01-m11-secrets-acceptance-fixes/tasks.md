## 1. Create retry budget (finding 1)

- [x] 1.1 Tests first (both SDKs, fake clock): budget is 60 s with 25 %
  jitter; gives up at exactly the budget without naming the secret; a name
  freed after 19.3/26.8/27.9/45 s is recreated with jitter 0, 0.5 and 1;
  delays are jittered by ±25 % and capped.
- [x] 1.2 `CREATE_RETRY_BUDGET_SECONDS`/`CREATE_RETRY_BUDGET_MS` = 60 s,
  `CREATE_RETRY_JITTER` = 0.25, injectable random, last sleep clipped.

## 2. destroy semantics (finding 2)

- [x] 2.1 Fakes mirror AWS (forced delete of a missing name succeeds).
- [x] 2.2 Tests first (both SDKs and both shims): never-existing → `False`
  with no `DeleteSecret`; existing → `True`; scheduled for deletion →
  `False`; concurrent not-found on delete → `False`; other errors propagate
  without the name.
- [x] 2.3 `destroy` = `DescribeSecret` + forced `DeleteSecret`; docstrings,
  TSDoc and IAM notes (`DescribeSecret` already in `RayitoSecretsAdmin`).

## 3. Logging and listing (findings 3 and 4)

- [x] 3.1 `test_secrets_e2e.py`: `caplog.set_level(logging.DEBUG,
  logger="rayito")`; budget assertion reuses the SDK constant.
- [x] 3.2 Docs: AWS SDK DEBUG logging prints values (Rayito never does);
  `list()` eventually consistent (~3–5 s), tests poll.

## 4. Gates and real AWS

- [x] 4.1 Python unit + ruff + mypy; TS lint/typecheck/build/test/pack:check;
  scripts tests; `check_pins`/`check_hygiene`; `make docs`.
- [x] 4.2 Real AWS (SDK side, Secrets Manager only): create → force-delete →
  re-create with the new budget (both SDKs); `destroy` of a never-existing
  name → `False`, of an existing one → `True` (both SDKs and shims); the
  Python and TypeScript secrets e2e on a `rayito-base` version built from
  this branch (deleted afterwards). Results in `AWS_API_NOTES.md` §19.
