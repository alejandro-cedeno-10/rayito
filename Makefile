.PHONY: proto build test test-python test-typescript test-sidecar test-e2e test-e2e-typescript test-bench lint lint-typescript limits fmt image-zip image-publish dev-hooks dev-run clean test-scripts bench-cold-start image-zip-slim image-publish-slim docs wheel image-publish-caps image-prune infra-lint sbom image-zip-poly image-publish-poly require-bucket

TARGET        := aarch64-unknown-linux-musl
RAYD_BIN      := target/$(TARGET)/release/rayd
IMAGE_ZIP     := image/rayito-image.zip
IMAGE_ZIP_SLIM := image/rayito-image-slim.zip
IMAGE_ZIP_POLY := image/rayito-image-poly.zip
PYTHON_CLIENT := clients/python
TS_CLIENT     := clients/typescript
SIDECAR       := kernel-sidecar
HOOKS_BASE    ?= http://127.0.0.1:9000
SIDECAR_PYTHON ?= $(SIDECAR)/.venv/bin/python
# Los shims de publish/prune corren dentro del entorno del cliente Python
# (rayito.cli, typer): mismo parser que `rayito image publish|prune`.
PY            := uv run --project $(PYTHON_CLIENT)
# Bucket de artefactos de los objetivos image-publish*: sin default, cada
# cuenta usa el suyo. BUCKET=<tu-bucket> en la línea de make o RAYITO_BUCKET
# exportado (make lee el entorno, y es la misma variable que lee la CLI); sin
# ninguno de los dos, require-bucket para el publish antes de compilar.
BUCKET        ?= $(RAYITO_BUCKET)
PUBLISH_ARGS  ?=
PRUNE_ARGS    ?= --keep 5
EGRESS_TEMPLATE := infra/egress-connector.yaml
CI_OIDC_TEMPLATE := infra/ci-oidc-role.yaml
IAM_TEMPLATE  := spike/m0/iam.yaml
SBOM          := crates/rayd/rayd.cdx.json
# Versión de la imagen base gestionada (`baseImageVersion` de create/update-
# microvm-image): el `imageVersion` más nuevo que devuelve
# `aws lambda-microvms list-managed-microvm-image-versions --image-identifier
# arn:aws:lambda:<region>:aws:microvm-image:al2023-1` (`1` hoy, `0` también
# existe). Si la API rechazara esta grafía, el valor pasa a la que ella
# misma devuelve en get-microvm-image-version (`1.0`); AWS_API_NOTES.md Q52
# registra lo medido. Se pasa SIEMPRE: scripts/publish_image.py lo exige.
BASE_IMAGE_VERSION ?= 1
BENCH_ARGS    ?=
BENCH_OUT     ?= docs/benchmarks/raw

# Regenera los clientes Python/TypeScript desde proto/ (Rust se regenera solo en
# cargo build vía crates/rayito-proto/build.rs, sin protoc).
proto:
	buf lint
	buf generate

# Binario estático ARM64 del agente. Nunca se compila dentro del Dockerfile.
# Con cargo-auditable en el PATH (`cargo install --locked cargo-auditable@0.7.6`)
# el grafo exacto de crates queda embebido en la sección ELF `.dep-v0` y
# scripts/check_auditable.py lo verifica; sin él, `cargo zigbuild` normal.
build:
	@if command -v cargo-auditable >/dev/null 2>&1; then \
	  echo "build: cargo auditable zigbuild (.dep-v0 embebido)"; \
	  cargo auditable zigbuild --release --locked --target $(TARGET) -p rayd && \
	  python scripts/check_auditable.py $(RAYD_BIN); \
	else \
	  echo "build: cargo zigbuild sin cargo-auditable (instala cargo-auditable@0.7.6 para embeber .dep-v0)"; \
	  cargo zigbuild --release --locked --target $(TARGET) -p rayd; \
	fi
	@ls -la $(RAYD_BIN)

# SBOM CycloneDX 1.5 del grafo de crates de rayd para el target ARM64
# (`cargo install --locked cargo-cyclonedx@0.5.9`). Se escribe en crates/rayd/
# (nombre por defecto de la herramienta; la herramienta escribe también los de
# rayd-core y rayito-proto, ignorados por .gitignore) y NUNCA bajo image/:
# image_zip.py sólo excluye .zip/.pyc y un JSON ahí entraría en el artefacto.
sbom:
	cargo cyclonedx --manifest-path crates/rayd/Cargo.toml --target $(TARGET) --format json --no-build-deps --spec-version 1.5
	@ls -la $(SBOM)

test:
	cargo test --workspace
	$(MAKE) test-python
	$(MAKE) test-typescript

test-python:
	@if [ -f $(PYTHON_CLIENT)/pyproject.toml ] && [ -d $(PYTHON_CLIENT)/tests/unit ]; then \
	  cd $(PYTHON_CLIENT) && uv run pytest tests/unit; \
	else \
	  echo "test-python: $(PYTHON_CLIENT) sin tests unitarios todavía, se omite"; \
	fi
	$(MAKE) test-scripts

# Tests unitarios de scripts/ (bench_cold_start, check_auditable, check_license
# y los shims image_zip/copy_sidecar/publish_image/image_prune): corren con el
# entorno del SDK, sin red. La lógica de los shims se prueba en
# clients/python/tests/unit/cli.
test-scripts:
	cd $(PYTHON_CLIENT) && uv run pytest ../../scripts/tests -p no:cacheprovider

# Cliente TypeScript (clients/typescript): pnpm con lockfile congelado; se
# omite si el paquete no existe todavía.
test-typescript:
	@if [ -f $(TS_CLIENT)/package.json ]; then \
	  cd $(TS_CLIENT) && pnpm install --frozen-lockfile && pnpm typecheck && pnpm test; \
	else \
	  echo "test-typescript: $(TS_CLIENT) sin paquete todavía, se omite"; \
	fi

lint:
	buf lint
	cargo fmt --all --check
	cargo clippy --workspace --all-targets -- -D warnings
	@if command -v cargo-deny >/dev/null 2>&1; then cargo deny check; else echo "lint: cargo-deny no está en el PATH (cargo install --locked cargo-deny@0.20.2 o el binario de la release 0.20.2); se omite deny.toml"; fi
	@if command -v actionlint >/dev/null 2>&1; then actionlint -no-color; else echo "lint: actionlint no está en el PATH (release 1.7.12: https://github.com/rhysd/actionlint/releases); se omiten los workflows"; fi
	python scripts/check_pins.py
	python scripts/check_hygiene.py
	uvx ruff==0.16.7 check scripts
	python scripts/gen_limits.py --check
	python scripts/check_license.py
	@if [ -f $(PYTHON_CLIENT)/pyproject.toml ]; then cd $(PYTHON_CLIENT) && uv run ruff check . && uv run ruff format --check . && uv run mypy src tests; fi
	$(MAKE) lint-typescript

lint-typescript:
	@if [ -f $(TS_CLIENT)/package.json ]; then \
	  cd $(TS_CLIENT) && pnpm install --frozen-lockfile && pnpm lint; \
	else \
	  echo "lint-typescript: $(TS_CLIENT) sin paquete todavía, se omite"; \
	fi

# Regenera _limits.py y limits.ts desde limits.json (fuente única de los
# límites de la API que validan ambos SDKs).
limits:
	python scripts/gen_limits.py

fmt:
	cargo fmt --all
	uvx ruff==0.16.7 format scripts

# Zip con el Dockerfile en la raíz, el binario precompilado y el sidecar (sin
# tests ni cachés) al lado, listo para subir a S3 y pasar como --code-artifact
# a create/update-microvm-image.
image-zip: build
	cp $(RAYD_BIN) image/rayd
	python scripts/copy_sidecar.py $(SIDECAR) image/kernel-sidecar
	python scripts/image_zip.py image $(IMAGE_ZIP)

# Variante slim (misma imagen, warm-up del kernel desactivado por el marcador
# `warmup_variant` que sólo existe dentro del zip): imagen de medición
# `rayito-base-slim`, no un template de producto.
image-zip-slim: build
	cp $(RAYD_BIN) image/rayd
	python scripts/copy_sidecar.py $(SIDECAR) image/kernel-sidecar
	python scripts/image_zip.py image $(IMAGE_ZIP_SLIM) --variant slim

# Primer prerrequisito de cada image-publish*: se expande al ejecutarse, así
# que sólo exige el bucket a quien publica y para antes de `cargo zigbuild`.
require-bucket:
	$(if $(strip $(BUCKET)),,$(error falta el bucket de artefactos: pasa BUCKET=<tu-bucket> o exporta RAYITO_BUCKET))

# Sube el zip a S3 (clave por sha256, no-op si existe), crea o actualiza la
# imagen rayito-base y espera al gate de tres estados. Requiere AWS_PROFILE y
# AWS_REGION; PUBLISH_ARGS admite p. ej. `--force` o `--image-name`.
# Equivale a `rayito image publish ...` (scripts/publish_image.py es un shim).
image-publish: require-bucket image-zip
	$(PY) python scripts/publish_image.py --artifact $(IMAGE_ZIP) --bucket $(BUCKET) --base-image-version $(BASE_IMAGE_VERSION) $(PUBLISH_ARGS)

image-publish-slim: require-bucket image-zip-slim
	$(PY) python scripts/publish_image.py --artifact $(IMAGE_ZIP_SLIM) --variant slim --bucket $(BUCKET) --base-image-version $(BASE_IMAGE_VERSION) $(PUBLISH_ARGS)

# Variante poly (M7): mismo Dockerfile, marcador `kernels_variant` sólo dentro
# del zip; la capa condicional instala el kernel bash (`javascript` queda
# reservado: ijavascript necesita compilador en al2023 ARM64, AWS_API_NOTES.md
# Q57) y el sidecar lo arranca en la primera celda de ese lenguaje. Imagen
# aparte `rayito-base-poly`; rayito-base no cambia.
image-zip-poly: build
	cp $(RAYD_BIN) image/rayd
	python scripts/copy_sidecar.py $(SIDECAR) image/kernel-sidecar
	python scripts/image_zip.py image $(IMAGE_ZIP_POLY) --variant poly

image-publish-poly: require-bucket image-zip-poly
	$(PY) python scripts/publish_image.py --artifact $(IMAGE_ZIP_POLY) --variant poly --bucket $(BUCKET) --base-image-version $(BASE_IMAGE_VERSION) $(PUBLISH_ARGS)

# Variante con capabilities (mismo zip que image-publish, imagen aparte
# `rayito-base-caps` con additionalOsCapabilities ALL): rayd instala en el
# arranque la ruta de política (`ip rule uidrange` + blackhole) que bloquea
# IMDS para todo uid distinto de 0.
image-publish-caps: require-bucket image-zip
	$(PY) python scripts/publish_image.py --artifact $(IMAGE_ZIP) --os-capabilities ALL --image-name rayito-base-caps --bucket $(BUCKET) --base-image-version $(BASE_IMAGE_VERSION) $(PUBLISH_ARGS)

# Borra versiones antiguas de rayito-base de una en una (espera a que la
# imagen salga de UPDATING/DELETING entre borrados). Primero `--dry-run`.
# Equivale a `rayito image prune ...` (scripts/image_prune.py es un shim).
image-prune:
	$(PY) python scripts/image_prune.py --image-name rayito-base $(PRUNE_ARGS)

# Valida las plantillas de infra/ (conector de egress y rol OIDC del e2e) y
# el IAM del spike (`PersistenceBucket`/`PersistencePrefix` de M7):
# validate-template (servidor, gratis) + cfn-lint. cfn-lint 1.56.3 ya conoce
# AWS::Lambda::NetworkConnector; si una versión anterior no lo conociera,
# añadir `--ignore-checks E3006` sólo para esa ejecución (infra/README.md).
infra-lint:
	aws cloudformation validate-template --template-body file://$(EGRESS_TEMPLATE) >/dev/null && echo "validate-template ok: $(EGRESS_TEMPLATE)"
	aws cloudformation validate-template --template-body file://$(CI_OIDC_TEMPLATE) >/dev/null && echo "validate-template ok: $(CI_OIDC_TEMPLATE)"
	aws cloudformation validate-template --template-body file://$(IAM_TEMPLATE) >/dev/null && echo "validate-template ok: $(IAM_TEMPLATE)"
	uvx cfn-lint==1.56.3 --version
	uvx cfn-lint==1.56.3 -- $(EGRESS_TEMPLATE) $(CI_OIDC_TEMPLATE) $(IAM_TEMPLATE)

# Aceptación contra AWS real (~$0.03 por sandbox). Se niega a correr sin las
# dos variables; RAYITO_EXECUTION_ROLE_ARN activa los logs de runtime.
test-e2e:
	@if [ "$$RAYITO_E2E" != "1" ] || [ -z "$$RAYITO_TEMPLATE" ]; then \
	  echo "test-e2e: exporta RAYITO_E2E=1 y RAYITO_TEMPLATE=<arn|nombre>"; exit 2; \
	fi
	cd $(PYTHON_CLIENT) && uv run pytest tests/e2e -m e2e -v -s

# La misma aceptación a través del SDK TypeScript (tests/e2e/m6.e2e.test.ts).
test-e2e-typescript:
	@if [ "$$RAYITO_E2E" != "1" ] || [ -z "$$RAYITO_TEMPLATE" ]; then \
	  echo "test-e2e-typescript: exporta RAYITO_E2E=1 y RAYITO_TEMPLATE=<arn|nombre>"; exit 2; \
	fi
	cd $(TS_CLIENT) && pnpm install --frozen-lockfile && pnpm test:e2e

# Benchmarks nightly contra AWS real (marker `bench`): 50 MB de escritura +
# lectura con los MB/s en el log. Mismas variables que test-e2e.
test-bench:
	@if [ "$$RAYITO_E2E" != "1" ] || [ -z "$$RAYITO_TEMPLATE" ]; then \
	  echo "test-bench: exporta RAYITO_E2E=1 y RAYITO_TEMPLATE=<arn|nombre>"; exit 2; \
	fi
	cd $(PYTHON_CLIENT) && uv run pytest tests/e2e -m bench -v -s

# Benchmark de cold start (M6, docs/benchmarks/): ~112 MicroVMs cortos y 20
# ciclos suspend/resume contra AWS real, JSON crudo en docs/benchmarks/raw/.
# Mismas variables que test-e2e; RAYITO_TEMPLATE_SLIM añade la fase (e);
# BENCH_ARGS admite p. ej. `--dry-run`, `--phases a,c` o `--sequential 5`.
bench-cold-start:
	@if [ "$$RAYITO_E2E" != "1" ] || [ -z "$$RAYITO_TEMPLATE" ]; then \
	  echo "bench-cold-start: exporta RAYITO_E2E=1 y RAYITO_TEMPLATE=<arn|nombre>"; exit 2; \
	fi
	cd $(PYTHON_CLIENT) && uv run python ../../scripts/bench_cold_start.py \
	  --template "$$RAYITO_TEMPLATE" \
	  $${RAYITO_TEMPLATE_SLIM:+--template-slim "$$RAYITO_TEMPLATE_SLIM"} \
	  $${RAYITO_EXECUTION_ROLE_ARN:+--execution-role-arn "$$RAYITO_EXECUTION_ROLE_ARN"} \
	  --out ../../$(BENCH_OUT) $(BENCH_ARGS)

# Bucle interno: arranca un rayd local aparte (make dev-run) y recorre los
# seis hooks en el orden de la plataforma.
dev-hooks:
	python scripts/hooks-sim.py --base-url $(HOOKS_BASE)

# rayd local con el sidecar real (Linux/WSL2; los pines instalados en
# kernel-sidecar/.venv con `uv venv && uv pip install -r requirements.txt`).
dev-run:
	mkdir -p /tmp/rayito-k
	cargo run -p rayd -- --sidecar-root $(SIDECAR) --socket-root /tmp/rayito-k \
	  --sidecar-cmd "$(SIDECAR_PYTHON) -m rayito_kernel_sidecar"

# Wheel + sdist del SDK Python con las comprobaciones de release.yml
# (contenido de la wheel y `twine check`); no publica nada.
wheel:
	cd $(PYTHON_CLIENT) && uv build
	python scripts/check_wheel.py $(PYTHON_CLIENT)/dist/*.whl
	uvx twine==7.0.0 check $(PYTHON_CLIENT)/dist/*

# Sitio de documentación (mkdocs-material + mkdocstrings) construido en modo
# estricto desde el entorno del cliente Python, sin deploy.
docs:
	cd $(PYTHON_CLIENT) && uv run --group docs mkdocs build -f ../../docs/site/mkdocs.yml --strict --site-dir ../../docs/site/_build

clean:
	cargo clean
	rm -rf image/rayd image/kernel-sidecar $(IMAGE_ZIP) $(IMAGE_ZIP_SLIM) $(IMAGE_ZIP_POLY)
