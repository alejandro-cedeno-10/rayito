## 1. Proxy (CLI-PROXY-01, CLI-PROXY-02)

- [x] 1.1 Tests driving `handle_connection` with a fake connector: foreign Host, missing Host, foreign Origin (plain and upgrade), `Origin: null`, allowed hosts/origins, full budget, slot release
- [x] 1.2 `ProxyAccess`/`build_access`, 421/403/503 responses, `--allowed-host`, `--allow-origin`, `--max-connections`, wildcard bind requires `--allowed-host`, `<id>.localhost` announcement
- [x] 1.3 Move `http_authority`/`is_wildcard` to `rayito/_authority.py`

## 2. Templates (TPL-SYMLINK-01)

- [x] 2.1 Python tests (file and directory symlink, image zip) and TS tests
- [x] 2.2 `_walk_regular_files` and `_artifact.shipped_files` skip symlinks

## 3. Git (GIT-CREDS-01)

- [x] 3.1 Tests: restore timeout keeps the operation error and warns, credentialed set-url timeout still restores, clone timeout strips, clone exit does not, URL rewrite refusal, isolation args (Python sync/async, TS)
- [x] 3.2 Implementation in `_git_base.py`, `sandbox_sync/git.py`, `sandbox_async/git.py`, `git-args.ts`, `git.ts`

## 4. CLI output (CLI-TERM-01)

- [x] 4.1 Tests for `visible_controls`/`echo` (TTY and pipe) and for the strict logs fallback and its warning
- [x] 4.2 Implementation in `_console.py`, `_logs.py`, `sandbox logs`

## 5. AWS errors (AWS-ERR-01)

- [x] 5.1 Tests with an `InvalidSignatureException` carrying the canonical string (Python and vitest)
- [x] 5.2 Stacks, `submit_build`/`submitBuild`, TS build adapter, `client_error_message`

## 6. Output cap (OUT-DOS-01)

- [x] 6.1 `commandOutputMaxBytes` and `accessTokenMinBytes` in `limits.json`, regenerated modules
- [x] 6.2 Tests for the cap, truncation flag, zero cap, PTY bound, validation (both SDKs)
- [x] 6.3 `DecodedStream`/`OutputAccumulator`, `max_output_bytes`/`maxOutputBytes`, `truncated`, CLI exec stream-only

## 7. Tokens (REPR-01, TOKEN-ENTROPY-01)

- [x] 7.1 Repr/inspect tests and the redaction
- [x] 7.2 Minimum-length tests and the check

## 8. Docs and gates

- [x] 8.1 `SECURITY.md`, `security.md`, `proxy-local.md`, `cli.md`, `git.md`, `templates.md`, `comandos.md`, `variables-de-entorno.md`
- [x] 8.2 CHANGELOG `[Unreleased]` → `Security` in both packages
- [x] 8.3 Python, TypeScript, docs, OpenSpec and root gates
