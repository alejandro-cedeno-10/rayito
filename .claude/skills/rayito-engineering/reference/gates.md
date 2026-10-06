# Gates locales

Son las mismas cadenas `run:` de `.github/workflows/ci.yml`; si cambian allí,
cambian también aquí y en `CONTRIBUTING.md` §3. Corre los del lenguaje que
tocaste, y además OpenSpec, docs y los de la raíz. Indica en el PR en qué
sistema corrieron.

## Contenido

- Rust (`rayd`) en una VM Linux
- Python
- TypeScript
- Docs, OpenSpec y raíz
- Cuándo hace falta cada uno

## Rust (`rayd`) en una VM Linux

`rayd` no compila en macOS. En macOS se usa una VM Lima con Ubuntu 24.04 y
un usuario sin privilegios con uid ≥ 1000, porque los tests de integración lo
exigen. Lima monta tu home en sólo lectura en la misma ruta: `cargo` solo
escribe en `CARGO_HOME` y en `CARGO_TARGET_DIR`, ambos en el disco de la VM.

Convenciones de la VM:

- Usuario `tester` (uid 1500).
- `CARGO_HOME=/var/tmp/tester-cargo`, compartido entre ramas.
- `RUSTUP_HOME=$HOME/.rustup` (el home de `tester`), con el toolchain de
  `rust-toolchain.toml`.
- Un `CARGO_TARGET_DIR` por rama (`/var/tmp/tester-target-<rama>`) y
  `CARGO_INCREMENTAL=0`.
- cargo-about 0.9.2 (para `make licenses` y `make image-zip*`) en
  `/var/tmp/tester-tools/cargo-about/`, en el PATH de `tester` sin pasos
  extra: `/etc/profile.d/rayito-tester-tools.sh` lo añade en shells de
  login (`sudo -u tester -i`) y el enlace `/usr/local/bin/cargo-about` lo
  cubre en `sudo -u tester -H bash <script>`. Comprobar con
  `limactl shell <vm> -- sudo -u tester -i bash -lc 'cargo-about --version'`.
- `-j 2`, porque el linker se queda sin memoria con más jobs.
- Todo `cargo` va dentro de `flock /var/tmp/rayito-cargo.lock`: puede haber
  otras sesiones compilando en la misma VM, y el lock las serializa.
- No pares la VM. Si el disco se llena, borra solo los `target` de ramas ya
  fusionadas (o reutiliza uno renombrándolo para tu rama).

Plantilla: escribe un script en la VM y lánzalo en segundo plano, guardando
los logs.

```bash
#!/bin/bash
set -o pipefail
cd <ruta-del-worktree>
export CARGO_HOME=/var/tmp/tester-cargo RUSTUP_HOME=$HOME/.rustup \
       CARGO_TARGET_DIR=/var/tmp/tester-target-<rama> CARGO_INCREMENTAL=0
export PATH=$RUSTUP_HOME/toolchains/<toolchain>-aarch64-unknown-linux-gnu/bin:$PATH
flock /var/tmp/rayito-cargo.lock bash -c "
cargo fmt --all --check > /var/tmp/<rama>-fmt.log 2>&1; echo FMT_EXIT=\$?
cargo clippy -j 2 --workspace --all-targets --locked -- -D warnings > /var/tmp/<rama>-clippy.log 2>&1; echo CLIPPY_EXIT=\$?
cargo test -j 2 --workspace --locked --no-fail-fast > /var/tmp/<rama>-test.log 2>&1; echo TEST_EXIT=\$?
"
```

Si cambia `Cargo.lock`, `about.toml` o `about.hbs`, `make licenses`
(cargo-about 0.9.2 en el PATH, el binario de su release con el sha256 de
`.github/actions/cargo-about`) genera `THIRD_PARTY_LICENSES.md` y comprueba
que cubre exactamente `cargo tree`. El fichero no se versiona: no hay nada
que commitear. Corre igual en la VM, dentro del mismo `flock`.

```bash
limactl shell <vm> -- sudo -u tester -H bash /var/tmp/<script>.sh
```

`m9_egress` necesita root con `CAP_NET_ADMIN` en un network namespace nuevo
y, si no lo tiene, se salta solo. CI lo ejecuta así
(`sudo env RAYITO_REQUIRE_EGRESS_NETNS=1 unshare --net -- <binario>`, ver
`CONTRIBUTING.md`). `buf lint` y `buf breaking` corren en CI cuando cambia el
`.proto`.

## Python

```bash
cd clients/python
uv run pytest tests/unit
uv run ruff check .
uv run ruff format --check .
uv run mypy src tests
uv run pytest ../../scripts/tests ../../infra/lambdas/events_webhooks/tests -p no:cacheprovider
```

Versiones declaradas, sin tocar `.venv` (CI: el job `python-versions`):

```bash
uv run --isolated --python 3.11 pytest tests/unit
uv run --isolated --python 3.13 pytest tests/unit
```

Paquete: `make wheel` desde la raíz (`uv build`, `check_wheel.py` y
`twine check`, con twine instalado desde
`.github/release/requirements-twine.txt` con `--hash`; nunca `uvx twine`).

## TypeScript

```bash
cd clients/typescript
pnpm install --frozen-lockfile
pnpm lint
pnpm typecheck
pnpm build
pnpm test
pnpm pack:check
python3 ../../scripts/check_docs_examples.py --typescript
```

`pack:check` comprueba el tarball, que no haya imports de
`@opentelemetry/api` en tiempo de ejecución, la forma del mapa `exports`
(tipos por condición) y los bloques "Coste y activación" de `dist/*.d.mts`.
Solo `pnpm` (nunca `npm` ni `yarn`), salvo el `npm publish` de `release.yml`.

## Docs, OpenSpec y raíz

```bash
cd clients/python && uv run --group docs mkdocs build -f ../../docs/site/mkdocs.yml --strict
cd clients/python && uv run --group dev python ../../scripts/check_docs_examples.py --ruff --mypy --cli
npx -y @fission-ai/openspec@1.10.0 validate --all --strict --no-interactive
python3 scripts/check_hygiene.py   # más tu lista privada en .git/info/hygiene-denylist
gitleaks git --log-opts=HEAD --redact --no-banner .   # 8.30.1, como leaks.yml
python3 scripts/check_pins.py
python3 scripts/gen_limits.py --check
python3 scripts/gen_stack_assets.py --check
python3 scripts/check_license.py
uvx ruff==0.16.7 check scripts
actionlint                       # si tocaste .github/workflows
make infra-lint                  # si tocaste infra/ (cfn-lint con --hash)
```

Los scripts de la raíz necesitan Python ≥ 3.11 (usan `tomllib`). Si el
`python3` del sistema es más antiguo, usa `uv run --no-project --python 3.12
python scripts/<script>.py`. Un fichero nuevo aún sin versionar no lo ve
`check_hygiene.py` sin argumentos: pásalo explícitamente
(`python3 scripts/check_hygiene.py $(git ls-files --others --exclude-standard)`).

## Cuándo hace falta cada uno

| Tocaste | Gates |
|---|---|
| `crates/`, `proto/` | Rust en la VM, más Python y TypeScript si cambió el proto (`make proto`) |
| `Cargo.lock`, `about.toml`, `about.hbs` | `make licenses` (genera y comprueba; no se versiona) |
| `clients/python/` | Python y docs (mkdocstrings lee los docstrings) |
| `clients/typescript/` | TypeScript |
| `docs/site/` | docs y ejemplos |
| `openspec/` | OpenSpec |
| `.github/workflows/` | `actionlint`, `check_pins.py` |
| Cualquier fichero | `check_hygiene.py` |
