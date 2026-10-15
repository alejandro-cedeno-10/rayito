## ADDED Requirements

### Requirement: `rayito dashboard` is off by default and read-only by default
The command SHALL bind to `127.0.0.1` on a random port, use the user's AWS credentials, and make no AWS call and load no optional peer unless it is run. Actions SHALL require `--allow-actions` and UI confirmation.

#### Scenario: no activation, no calls
- **WHEN** the SDK and CLI are used without running `rayito dashboard`
- **THEN** the golden call trace contains no dashboard-related AWS call

#### Scenario: actions refused without the flag
- **WHEN** a kill request reaches the server started without `--allow-actions`
- **THEN** it is rejected and no AWS call is made

### Requirement: The local server defends against browser attacks
The server SHALL check `Host` and `Origin`, reject `/api/v1` requests whose `Sec-Fetch-Site` is present and neither `same-origin` nor `none`, protect state-changing requests and the paid billed-cost query with a CSRF token, and deliver a one-time token in the start URL that the server swaps for an `HttpOnly`, `SameSite=Strict`, `Path=/` cookie before redirecting to a clean URL, with `Referrer-Policy: no-referrer`. The printed start URL SHALL be the only place the token is shown and it SHALL NOT be logged.

#### Scenario: start token is single use
- **WHEN** the start URL token is presented a second time
- **THEN** it is rejected

#### Scenario: rebinding and cross-origin requests are rejected
- **WHEN** a request arrives with a foreign `Host` or `Origin`, or without the session token
- **THEN** it is rejected

#### Scenario: a same-site request without Origin is rejected
- **WHEN** a request with a valid session cookie arrives without `Origin` and with `Sec-Fetch-Site: same-site` (for example an `<img>` served by a sandbox on another loopback port)
- **THEN** it is rejected with 403

#### Scenario: the paid GET needs the CSRF token
- **WHEN** `GET /api/v1/cost?billed=true` arrives with a valid session cookie but without the CSRF header
- **THEN** it is rejected with 403 and Cost Explorer is not called

### Requirement: The SPA needs no build step
The SPA SHALL be ES modules and web components served from the wheel, with an overview page first (metric cards, usage charts drawn as inline SVG), a dense sandbox table, dark theme by default, a light theme and an "auto" mode that follows the operating system. It SHALL draw every colour, radius, font and spacing from CSS custom properties (design tokens) and contain no external library, font, image or remote URL.

#### Scenario: installed wheel serves the SPA
- **WHEN** the dashboard starts from an installed wheel
- **THEN** the SPA is served without Node or a build step

#### Scenario: the SPA works under the unchanged CSP
- **WHEN** any page is loaded
- **THEN** it uses no inline script or style and the `Content-Security-Policy` is the same as without a user theme

### Requirement: The panel accepts a validated user theme and logo
`rayito dashboard --theme <file>` (also `RAYITO_DASHBOARD_THEME`) SHALL load a `.json` or `.css` file of token values, and `--logo <file>` (also `RAYITO_DASHBOARD_LOGO`) a PNG or SVG. Both SHALL be validated before the port opens: unknown keys are rejected, colour, length and font values must match a strict grammar, and files over a named size cap are rejected. The served stylesheet SHALL be generated from the validated values, never copied from the file. An SVG with scripts, `foreignObject`, animation, event handlers, DTD or entities, or any reference that is not a local fragment SHALL be rejected, and the logo SHALL be served same-origin with its own restrictive CSP. Errors SHALL name the key, never the value or the path.

#### Scenario: a valid theme is served
- **WHEN** the panel starts with a valid theme file
- **THEN** `/theme.css` carries its values and `/api/v1/meta` reports its name

#### Scenario: injection attempts are rejected
- **WHEN** a theme value contains `url(`, a semicolon, a brace, markup or an `@import`, or a key is not in the token list
- **THEN** the command exits with code 2 before opening the port and the message does not repeat the value

#### Scenario: an unsafe logo is rejected
- **WHEN** the logo is an SVG with a script, an event handler or an external reference
- **THEN** the command exits with code 2

### Requirement: Bulk actions respect platform TPS
Bulk kill, suspend and resume SHALL be queued at or below the applied TPS of each operation and show progress.

#### Scenario: closing the panel stops the queue
- **WHEN** the panel is closed while a bulk job is running and other jobs are queued
- **THEN** the sandbox in flight finishes, no further sandbox is touched, and the remaining items are reported as `JobAborted`

#### Scenario: the queue is bounded
- **WHEN** a bulk action arrives while the maximum number of unfinished jobs is already queued or running
- **THEN** it is rejected with 429 `jobs_busy` and nothing is queued

#### Scenario: suspend queue honours 2 TPS
- **WHEN** 10 sandboxes are suspended in bulk with a 2 TPS limit
- **THEN** after an initial burst of 2 calls (the bucket capacity equals the rate), consecutive calls start at least 0.5 s apart, so the queue spans at least 4 s

### Requirement: The logs view shows the end of the stream
The panel SHALL read the newest lines of a sandbox log stream, in chronological order, and say when older lines exist. CloudWatch errors SHALL reach the API as SDK errors (a denied call is 403 `aws_access_denied`), never as a raw AWS exception.

#### Scenario: a long-running sandbox
- **WHEN** a sandbox has more log events than the line limit
- **THEN** the last lines are returned, oldest first, with `truncated` true

#### Scenario: logs permission missing
- **WHEN** CloudWatch denies the read
- **THEN** the API answers 403 `aws_access_denied` with the AWS code and no AWS message

### Requirement: The panel carries its own help and an illustrated guide
The page SHALL have a Help entry in the sidebar that lists the sections, says which options are on in this session and links to the published step-by-step guide, built from `/api/v1/meta` with no API call and no external request. The committed screenshots of the guide SHALL show only demo data and SHALL carry no image metadata nor exceed the size limit, and a repository check SHALL fail otherwise.

#### Scenario: help works offline
- **WHEN** the Help view is opened without network access
- **THEN** it renders the steps and the state of actions, billed cost and events from `meta`, and only the guide link leaves the page when clicked

#### Scenario: a screenshot with metadata is committed
- **WHEN** a dashboard image carries a text, EXIF or time chunk, is not a PNG, or exceeds the size limit
- **THEN** `scripts/check_dashboard_images.py` exits 1 naming the file
