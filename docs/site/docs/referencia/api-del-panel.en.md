# Management dashboard JSON API

The `/api/v1` API served by [`rayito dashboard`](../cli.md#rayito-dashboard)
and consumed by its page. It is the same for all the dashboard's shells: the
local server today and, in the next phase of the `dashboard` OpenSpec change,
the stack in your account. All of them return documents with the same shape,
which are validated against the JSON schema versioned in the repository
([`testdata/dashboard/api-v1.schema.json`](https://github.com/alejandro-cedeno-10/rayito/blob/main/testdata/dashboard/api-v1.schema.json)),
and the same contract tests run against each shell.

!!! note "It is not a public service API"
    There is no Rayito server: the API is served by a process of yours, on
    `127.0.0.1`, while the dashboard runs. It exists for the page and for
    testing it; write your scripts with the SDK services
    ([`Quotas`, `UsageCost` and `Fleet`](../funciones-opcionales/panel-de-gestion.md)).

## Access (local shell) { #acceso-cascara-local }

All routes require the session cookie that the server hands out when the
bootstrap token is redeemed ([Security](../security.md#panel-de-gestion-local));
`POST` requests additionally require `Content-Type: application/json` and the
`X-Rayito-CSRF` header with the token from `GET /api/v1/session`, a route that
only the local shell has. `GET /api/v1/cost` with `billed=true` also requires
that header, because each query is billed. A request with `Sec-Fetch-Site`
other than `same-origin` or `none` receives `403`.

## Versioning { #versionado }

The version is in the path (`/api/v1`). Within `v1` only fields and routes
are added; removing or changing one requires `/api/v2`. Keys are in
`camelCase`, dates in ISO 8601 UTC and amounts in USD.

## Reads { #lectura }

| Route | Parameters | Response |
|---|---|---|
| `GET /api/v1/meta` | | API and Rayito version, region, `allowActions`, `costExplorer` (`enabled`, `requestPriceUsd` and `cacheTtlSeconds`), `eventsConfigured`, `logsAvailable` and the intervals and caps the page uses |
| `GET /api/v1/sandboxes` | `state` (repeatable), `image` | `sandboxes[]` with `sandboxId`, `state`, `image`, `imageArn`, `imageVersion`, `startedAt`, `memoryMib` |
| `GET /api/v1/images` | `filter` | `images[]` |
| `GET /api/v1/images/versions` | `image` (required) | `versions[]`, newest first |
| `GET /api/v1/quotas` | | `entries[]` (quotas with their `increaseUrl`), `memory` (allocated, quota, fraction and `level`), `tps[]`, `warnings[]` and `thresholds` |
| `GET /api/v1/cost` | `billed=true`, `start`, `end`, `region`, `refresh` | `estimated` always and `billed` only with `billed=true` |
| `GET /api/v1/events` | `sandboxId`, `type` (repeatable), `limit`, `order` | `configured` and `events[]` |
| `GET /api/v1/accounts` | | `accounts[]` with the `name` of each additional account of the multi-account view (`rayito dashboard --account`); empty without it. With `?account=NAME` on any other read route, that account is requested (always read-only) |
| `GET /api/v1/logs` | `sandboxId` (required), `limit` | the last `limit` lines in chronological order, `lines[]` (`timestamp`, `message`, `stream`), `truncated` if there are earlier ones and `fallback` if the expected stream did not exist and the lines come from streams found by name (they may not be the sandbox's) |

Cost comes in **two** documents that are never mixed: `estimated` (with
`label: "estimated"`, the dashboard's calculation) and `billed` (with
`label: "billed"`, from Cost Explorer, with `partial` if AWS is still
estimating some period). `billed=true` without `--cost-explorer` is
`403 cost_explorer_disabled` and makes no paid query.

```json
{
  "sandboxes": [
    {
      "sandboxId": "microvm-00000000-0000-0000-0000-000000000001",
      "state": "RUNNING",
      "image": "rayito-base-2gb",
      "imageArn": "arn:aws:lambda:us-east-1:123456789012:microvm-image:rayito-base-2gb",
      "imageVersion": "1.0",
      "startedAt": "2026-10-13T12:00:00Z",
      "memoryMib": 2048
    }
  ]
}
```

No response carries `envs`, `runHookPayload`, tokens or JWEs: they do not
exist in the domain model.

## Actions { #acciones }

Only with `--allow-actions`; without it, all of them are `403 actions_disabled`
and do not touch AWS. All require `"confirm": true` in the body and return
`202` with the queued job (`job`).

| Route | Body | What it does |
|---|---|---|
| `POST /api/v1/sandboxes/{id}/{action}` | `{"confirm": true}` | `action` is `kill`, `suspend` or `resume` on one sandbox |
| `POST /api/v1/actions` | `{"action": "suspend", "sandboxIds": ["..."], "confirm": true}` | the same action on several (up to `bulkMaxSandboxes` from `/meta`) |
| `GET /api/v1/actions` | | the latest jobs, most recent first |
| `GET /api/v1/actions/{jobId}` | | one job: `status` (`queued`, `running`, `finished`), `completed`, `total`, `counts` and `items[]` |
| `GET /api/v1/actions/plan` | `action`, `count` | how long it would take: `plan.tps` and `plan.estimatedSeconds` |

A single worker runs the jobs one at a time, at the pace of the operation's
TPS quota (the applied one, if lower than the documented one). Each
`items[]` entry carries `outcome` (`done`, `unchanged` or `failed`) and, if it
failed, the AWS **code** in `code`; never the message.

## Errors { #errores }

Every error is `{"error": {"code", "message"}}` and, if it comes from AWS,
`awsCode`. The message is our own and never repeats AWS's. The `code`s are a
closed list:

| `code` | HTTP | When |
|---|---|---|
| `invalid_argument`, `invalid_json`, `confirmation_required` | 400 | invalid parameter or body; `confirm` missing |
| `unauthorized` | 401 | no session or bootstrap token already spent |
| `forbidden`, `csrf_failed`, `actions_disabled`, `cost_explorer_disabled`, `aws_access_denied` | 403 | foreign `Origin`, CSRF, action or cost not enabled, or AWS rejected the credentials |
| `not_found`, `method_not_allowed` | 404, 405 | unknown route or job; method not allowed |
| `payload_too_large`, `unsupported_media_type` | 413, 415 | body larger than the cap; not JSON |
| `misdirected_request` | 421 | foreign `Host` |
| `aws_throttled`, `jobs_busy` | 429 | AWS throttled; the dashboard's queue already has the maximum number of unfinished actions |
| `aws_error`, `internal_error` | 502, 500 | AWS returned another error; unexpected dashboard failure |

## See also { #ver-tambien }

- [Management dashboard](../funciones-opcionales/panel-de-gestion.md)
- [CLI: `rayito dashboard`](../cli.md#rayito-dashboard)
- [Security](../security.md#panel-de-gestion-local)
