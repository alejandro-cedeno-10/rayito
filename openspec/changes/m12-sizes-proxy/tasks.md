## 1. Docs: size is a property of the image (`sizing-docs`)

- [x] 1.1 `docs/site/docs/limits.md`: new "## Tamaño (CPU/RAM)" section before "## Compatibilidad SDK ↔ rayd ↔ imagen" — the verified `AWS_API_NOTES.md` table, the E2B `Template.build` analogy, the `--memory-mib`/`--image-name` recipe, the cost angle, and the `Q68` guest-view caveat.
- [x] 1.2 `docs/site/docs/images.md`: cross-reference in "## Publicar las tres".
- [x] 1.3 `docs/site/docs/e2b-compat.md`: split the cpu/memory footnote from the unrelated "CLI de templates/snapshots/fork" row and name the per-image alternative.
- [x] 1.4 `scripts/tests/test_m12_docs.py`: assert the section exists, cites `AWS_API_NOTES.md` and `Q68`, and sits before the compatibility table.
- [x] 1.5 `mkdocs build --strict` (`docs/site/mkdocs.yml`) clean.

## 2. Docs: e2b-parity.md rows 82, 110 and the status counts

- [x] 2.1 Row 82 → "divergente", note names `--memory-mib`, `Template.build`'s continued `UnimplementedError`, `Q68`, link to `limits.md#tamano-cpuram`.
- [x] 2.2 Row 110 note → points at `rayito sandbox proxy`; status stays "fuera por SPEC" (no ADR-014 amendment in this branch permits hosted ingress).
- [x] 2.3 Recompute the status-count table (18 divergente, 12 fuera por SPEC).
- [x] 2.4 `scripts/tests/test_m9_docs.py::test_the_parity_page_has_every_ledger_row` stays green; `scripts/tests/test_m12_docs.py` asserts rows 82/110 content.

## 3. CLI: `rayito sandbox proxy` (`clients/python/src/rayito/cli/_proxy.py`)

- [x] 3.1 Pure helpers: `validate_proxy_port` (rejects 9000 and out-of-range before any AWS call), `parse_http_head`/`rewrite_head` (strips client `x-aws-proxy-*`, sets `Host`/`X-aws-proxy-auth`/`X-aws-proxy-port`, forces `Connection: close` except on `Upgrade`), `is_loopback_bind`, `local_url`.
- [x] 3.2 `resolve_endpoint` (`get-microvm`, errors on a terminated sandbox) and `build_refresher` (reuses `rayito._transport.TokenRefresher`/`TokenStore`, always `PortSpec.single(port)`).
- [x] 3.3 Asyncio server: `handle_connection` (64 KiB header cap via the default `StreamReader` limit, injectable connector factory, bidirectional pipe until either side closes) and `serve_proxy`/`run_proxy` (sync CLI entry point, Ctrl-C stops the refresher and the listener).
- [x] 3.4 `default_connector`: TLS to `<endpoint>:443`, SNI = endpoint.
- [x] 3.5 Wire `proxy` into `rayito/cli/sandbox.py` after `connect`: `--port`, `--local-port` (defaults to `--port`), `--bind` (default `127.0.0.1`), `--allow-remote` (usage error without it off loopback).
- [x] 3.6 `docs/site/docs/cli.md`: new "### `proxy`" under "## `rayito sandbox`".
- [x] 3.7 `clients/python/tests/unit/cli/test_proxy.py`: header rewrite (pure), port rejection (9000 and out-of-range), the JWE store does not remint per read (only on TTL expiry, fake clock), an end-to-end server test against a fake loopback upstream (GET and Upgrade, injected connector, no TLS), `--bind 0.0.0.0` without `--allow-remote` (usage exit code), and a `caplog` assertion that the JWE never appears in a log record.
- [x] 3.8 `clients/python/tests/e2e/test_sandbox_proxy_e2e.py` (`@pytest.mark.e2e`, skipped without `RAYITO_E2E=1`): a real sandbox running `python3 -m http.server`, the proxy in a background thread, a `GET` from the test process.

## 4. Docs: SECURITY.md / security.md (`security-docs`)

- [x] 4.1 `SECURITY.md` T2 and T3: M12 appendix (single-port JWE scope, never port 9000, loopback default, `x-aws-proxy-*` stripped, never logged; residual risk, no new threat number).
- [x] 4.2 `docs/site/docs/security.md`: matching prose section, cross-linking `cli.md#proxy`.
- [x] 4.3 `scripts/tests/test_security_docs.py` stays green; `scripts/tests/test_m12_docs.py` asserts `SECURITY.md` and `security.md` mention `sandbox proxy`.

## 5. CHANGELOGs and gates

- [x] 5.1 `clients/python/CHANGELOG.md` [Unreleased]: `rayito sandbox proxy` under Added.
- [x] 5.2 `cd clients/python && uv run pytest tests/unit -q` green (2189 passed, unchanged plus the new `test_proxy.py`).
- [x] 5.3 `cd clients/python && uv run ruff check . && uv run ruff format --check . && uv run mypy src tests` clean on the touched files.
- [x] 5.4 `cd clients/python && uv run pytest ../../scripts/tests -q` green (`test_m12_docs.py` included).
- [x] 5.5 `python3 scripts/check_pins.py` and `python3 scripts/check_hygiene.py` clean.
- [x] 5.6 `openspec validate m12-sizes-proxy --strict` clean.
- [x] 5.7 Real-AWS e2e (`test_sandbox_proxy_e2e.py`) left for CI/manual acceptance with `RAYITO_E2E=1`; not run here (no real AWS in this environment, per the campaign's hard rule).
