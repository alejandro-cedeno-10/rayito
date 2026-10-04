## ADDED Requirements

### Requirement: Caller-supplied strings are parsed in linear time
The TypeScript SDK SHALL NOT apply to a caller-supplied string a regular expression that backtracks in super-linear time (CodeQL `js/polynomial-redos`). Trailing and leading runs of a single character SHALL be removed with `stripTrailing(text, char)`/`stripLeading(text, char)` from `src/strings.ts` (index loops), used by `decodeAccessToken` (padding `=`), `downloadFilename` and `deriveRepoDirFromUrl` (trailing `/`) and `DockerIgnore.fromText` (leading and trailing `/`). The http(s) URL split behind `withCredentials` and `stripCredentials` SHALL match the tail with `[\s\S]*` and treat a tail that contains a line terminator (`\n`, `\r`, U+2028, U+2029) as "not an http(s) URL", exactly the inputs the previous `(.*)$` rejected, so both functions keep their results. `tests/unit/linear-time-parsing.test.ts` SHALL run every call site on a 50 000-character adversarial input within 150 ms and pin the unchanged results.

#### Scenario: a long run of slashes does not block the event loop
- **WHEN** `files.download()` is called without `filename` on a path of 50 000 `/` followed by `x`
- **THEN** the file name is derived in well under 150 ms (the previous regex took over a second)

#### Scenario: a line terminator after the authority keeps its meaning
- **WHEN** `withCredentials("https://example.com/acme\napp.git", "bot", "pw")` is called
- **THEN** it throws `InvalidArgumentError` as before, and `stripCredentials` returns that URL unchanged
