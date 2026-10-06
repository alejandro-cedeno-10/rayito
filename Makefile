.PHONY: proto build test test-python test-typescript test-sidecar test-e2e test-e2e-typescript test-bench lint lint-typescript limits fmt image-zip image-publish dev-hooks dev-run clean test-scripts bench-cold-start image-zip-slim image-publish-slim docs wheel image-publish-caps image-prune infra-lint sbom image-zip-poly image-publish-poly image-zip-efs image-publish-caps-efs require-bucket release-pr docs-examples local-guest-context local-up local-e2e local-down licenses require-cargo-about image-licenses

TARGET        := aarch64-unknown-linux-musl
# Directorio de compilación efectivo (respeta CARGO_TARGET_DIR) y CARGO_HOME:
# `build` los reescribe en el binario como /target y /cargo.
BUILD_TARGET_DIR := $(abspath $(or $(CARGO_TARGET_DIR),target))
CARGO_HOME_DIR   := $(abspath $(or $(CARGO_HOME),$(HOME)/.cargo))
RAYD_BIN      := $(BUILD_TARGET_DIR)/$(TARGET)/release/rayd
IMAGE_ZIP     := image/rayito-image.zip
IMAGE_ZIP_SLIM := image/rayito-image-slim.zip
IMAGE_ZIP_POLY := image/rayito-image-poly.zip
IMAGE_ZIP_EFS := image/rayito-image-efs.zip
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
IAM_TEMPLATE  := infra/iam.yaml
SECRETS_TEMPLATE := infra/secrets-access.yaml
METADATA_INDEX_TEMPLATE := infra/metadata-index.yaml
CUSTOM_DOMAIN_TEMPLATE := infra/custom-domain.yaml
EVENTS_WEBHOOKS_TEMPLATE := infra/events-webhooks.yaml
EFS_VOLUMES_TEMPLATE := infra/efs-volumes.yaml
SBOM          := crates/rayd/rayd.cdx.json
# Avisos de terceros de rayd (openspec third-party-licenses): cargo-about
# fijado (el mismo binario y sha256 que ./.github/actions/cargo-about), la
# política de about.toml y la plantilla about.hbs sobre el grafo de
# Cargo.lock para aarch64-unknown-linux-musl. `--frozen`: sin red y sin
# tocar Cargo.lock; `cargo fetch --locked` deja antes las fuentes en
# CARGO_HOME. THIRD_PARTY_LICENSES.md no se versiona: se genera al empaquetar
# (`image-licenses`, y de ahí cada `image-zip*`, el job `build` de CI y
# `rayd-build` de la release), así que un cambio de Cargo.lock (p. ej. un PR
# de Dependabot) no pide ningún commit más. LICENSE, NOTICE y este fichero
# viajan juntos con cada copia de rayd: en el zip de la imagen
# (image/licenses/, que el Dockerfile copia a /usr/share/doc/rayd/) y como
# assets de la release.
THIRD_PARTY_LICENSES := THIRD_PARTY_LICENSES.md
CARGO_ABOUT_VERSION := 0.9.2
CARGO_ABOUT_ARGS := --frozen --fail -c about.toml -m crates/rayd/Cargo.toml about.hbs
LICENSE_FILES := LICENSE NOTICE $(THIRD_PARTY_LICENSES)
IMAGE_LICENSES := image/licenses
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
# Herramientas de Python con dependencias (cfn-lint, twine): se instalan desde
# su fichero de requisitos con --hash en un venv temporal que se borra al
# acabar, nunca con `uvx`, que resolvería su grafo transitivo al vuelo en cada
# ejecución (scripts/check_pins.py, puerta 2; sec-supply-chain-followups,
# SC-A03). Uso: $(call hashed-tool,<herramienta>,<venv>).
TOOL_REQUIREMENTS := .github/release
TOOL_PYTHON   := 3.12
hashed-tool = uv venv -q --python $(TOOL_PYTHON) $(2) && uv pip install -q --python $(2) --require-hashes --no-deps --only-binary :all: -r $(TOOL_REQUIREMENTS)/requirements-$(1).txt

# Regenera los clientes Python/TypeScript desde proto/ (Rust se regenera solo en
# cargo build vía crates/rayito-proto/build.rs, sin protoc).
proto:
	buf lint
	buf generate

# Binario estático ARM64 del agente. Nunca se compila dentro del Dockerfile.
# Con cargo-auditable en el PATH (`cargo install --locked cargo-auditable@0.7.6`)
# el grafo exacto de crates queda embebido en la sección ELF `.dep-v0` y
# scripts/check_auditable.py lo verifica; sin él, `cargo zigbuild` normal.
# `--remap-path-prefix` (vía `--config build.rustflags`, que se suma a los
# rustflags de los ficheros de configuración; un RUSTFLAGS exportado lo
# anula) quita del binario las rutas del constructor: las de los crates del
# registro en CARGO_HOME (ubicaciones de pánico) y la del directorio de
# compilación (código generado en OUT_DIR). `.dep-v0` no lleva rutas.
REMAP_CONFIG := --config 'build.rustflags=["--remap-path-prefix=$(CARGO_HOME_DIR)=/cargo","--remap-path-prefix=$(BUILD_TARGET_DIR)=/target"]'
build:
	@if command -v cargo-auditable >/dev/null 2>&1; then \
	  echo "build: cargo auditable zigbuild (.dep-v0 embebido)"; \
	  cargo auditable zigbuild --release --locked --target $(TARGET) -p rayd $(REMAP_CONFIG) && \
	  python scripts/check_auditable.py $(RAYD_BIN); \
	else \
	  echo "build: cargo zigbuild sin cargo-auditable (instala cargo-auditable@0.7.6 para embeber .dep-v0)"; \
	  cargo zigbuild --release --locked --target $(TARGET) -p rayd $(REMAP_CONFIG); \
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

# cargo-about exacto en el PATH: otra versión puede renderizar distinto, y
# lo que se genera tiene que ser lo mismo en local, en CI y en la release.
require-cargo-about:
	@cargo about --version 2>/dev/null | grep -qx "cargo-about $(CARGO_ABOUT_VERSION)" || { \
	  echo "falta cargo-about $(CARGO_ABOUT_VERSION): binario de https://github.com/EmbarkStudios/cargo-about/releases/tag/$(CARGO_ABOUT_VERSION) (sha256 en .github/actions/cargo-about) o cargo install --locked cargo-about@$(CARGO_ABOUT_VERSION)"; \
	  exit 1; }

# Genera THIRD_PARTY_LICENSES.md desde el Cargo.lock actual y falla si sus
# crates no son exactamente los que `cargo tree` (el resolver de cargo, sin
# cargo-about) compila en rayd para el target de release (job `build` de CI
# y `rayd-build` de release). Ni cargo-about ni `cargo tree` compilan nada ni
# ejecutan build scripts: leen `cargo metadata` y las fuentes que deja
# `cargo fetch`. Algunos crates publican su licencia con finales CRLF: se
# normaliza a LF para que la salida sea la misma en cualquier máquina.
licenses: require-cargo-about
	cargo fetch --locked
	@generated="$$(mktemp)" && tree="$$(mktemp)" && trap 'rm -f "$$generated" "$$tree"' EXIT && \
	  cargo about generate $(CARGO_ABOUT_ARGS) -o "$$generated" && \
	  tr -d '\r' < "$$generated" > $(THIRD_PARTY_LICENSES) && \
	  cargo tree --frozen -p rayd --target $(TARGET) -e normal --prefix none --format '{p}' > "$$tree" && \
	  python3 scripts/check_third_party_licenses.py $(THIRD_PARTY_LICENSES) --tree "$$tree"

# Deja LICENSE, NOTICE y un THIRD_PARTY_LICENSES.md recién generado en
# image/licenses/, que image/Dockerfile copia a /usr/share/doc/rayd/; el zip
# se niega a empaquetar un rayd sin ellos
# (rayito.cli._artifact.require_rayd_notices). Siempre regenera: nunca se
# empaqueta un fichero que se quedó atrás respecto al Cargo.lock.
image-licenses: licenses
	rm -rf $(IMAGE_LICENSES)
	mkdir -p $(IMAGE_LICENSES)
	cp $(LICENSE_FILES) $(IMAGE_LICENSES)/

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

# Deja el PR de release-please listo: lockfiles, CHANGELOG (Keep a Changelog)
# y un único commit firmado sobre origin/main (docs/RELEASING.md §6).
release-pr:
	python3 scripts/prepare_release_pr.py $(RELEASE_PR_ARGS)

# Tests unitarios de scripts/ (bench_cold_start, check_auditable, check_license
# y los shims image_zip/copy_sidecar/publish_image/image_prune): corren con el
# entorno del SDK, sin red. La lógica de los shims se prueba en
# clients/python/tests/unit/cli.
test-scripts:
	cd $(PYTHON_CLIENT) && uv run pytest ../../scripts/tests ../../infra/lambdas/events_webhooks/tests -p no:cacheprovider

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

# Zip con el Dockerfile en la raíz, el binario precompilado, sus avisos de
# licencia (image/licenses/) y el sidecar (sin tests ni cachés) al lado, listo para subir a S3 y pasar como --code-artifact
# a create/update-microvm-image.
image-zip: build image-licenses
	cp $(RAYD_BIN) image/rayd
	python scripts/copy_sidecar.py $(SIDECAR) image/kernel-sidecar
	python scripts/image_zip.py image $(IMAGE_ZIP)

# Variante slim (misma imagen, warm-up del kernel desactivado por el marcador
# `warmup_variant` que sólo existe dentro del zip): imagen de medición
# `rayito-base-slim`, no un template de producto.
image-zip-slim: build image-licenses
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

# Variante poly (M7, Deno en M9): mismo Dockerfile, marcador `kernels_variant`
# sólo dentro del zip; la capa condicional instala el kernel bash y Deno
# (`javascript` y `typescript`, ADR-013) y el sidecar arranca cada kernel en
# la primera celda de su lenguaje. Imagen aparte `rayito-base-poly`;
# rayito-base no cambia.
image-zip-poly: build image-licenses
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

# Variante caps con amazon-efs-utils (m15-efs-volumes, `volumes=`): mismo
# Dockerfile, marcador `efs_variant` sólo dentro del zip (`--with-efs`); la capa
# condicional instala efs-utils (+~198 MB de imagen, snapshot igual, Q122) y
# rehace el enlace de /usr/bin/python3. Imagen aparte `rayito-base-caps-efs`,
# siempre con additionalOsCapabilities ALL; las demás imágenes no cambian.
image-zip-efs: build image-licenses
	cp $(RAYD_BIN) image/rayd
	python scripts/copy_sidecar.py $(SIDECAR) image/kernel-sidecar
	python scripts/image_zip.py image $(IMAGE_ZIP_EFS) --with-efs

image-publish-caps-efs: require-bucket image-zip-efs
	$(PY) python scripts/publish_image.py --artifact $(IMAGE_ZIP_EFS) --with-efs --os-capabilities ALL --bucket $(BUCKET) --base-image-version $(BASE_IMAGE_VERSION) $(PUBLISH_ARGS)

# Borra versiones antiguas de rayito-base de una en una (espera a que la
# imagen salga de UPDATING/DELETING entre borrados). Primero `--dry-run`.
# Equivale a `rayito image prune ...` (scripts/image_prune.py es un shim).
image-prune:
	$(PY) python scripts/image_prune.py --image-name rayito-base $(PRUNE_ARGS)

# Valida las plantillas de infra/ (conector de egress, rol OIDC del e2e, el
# IAM de build/ejecución/cliente con los parámetros de persistencia y
# transferencias, las políticas opcionales de secretos de M13a y la tabla
# opcional del índice de metadatos de M14, y los volúmenes EFS de M15):
# validate-template (servidor, gratis) + cfn-lint (desde
# .github/release/requirements-cfn-lint.txt, con --hash). cfn-lint 1.56.3 ya conoce
# AWS::Lambda::NetworkConnector; si una versión anterior no lo conociera,
# añadir `--ignore-checks E3006` sólo para esa ejecución (infra/README.md).
infra-lint:
	aws cloudformation validate-template --template-body file://$(EGRESS_TEMPLATE) >/dev/null && echo "validate-template ok: $(EGRESS_TEMPLATE)"
	aws cloudformation validate-template --template-body file://$(CI_OIDC_TEMPLATE) >/dev/null && echo "validate-template ok: $(CI_OIDC_TEMPLATE)"
	aws cloudformation validate-template --template-body file://$(IAM_TEMPLATE) >/dev/null && echo "validate-template ok: $(IAM_TEMPLATE)"
	aws cloudformation validate-template --template-body file://$(SECRETS_TEMPLATE) >/dev/null && echo "validate-template ok: $(SECRETS_TEMPLATE)"
	aws cloudformation validate-template --template-body file://$(METADATA_INDEX_TEMPLATE) >/dev/null && echo "validate-template ok: $(METADATA_INDEX_TEMPLATE)"
	aws cloudformation validate-template --template-body file://$(CUSTOM_DOMAIN_TEMPLATE) >/dev/null && echo "validate-template ok: $(CUSTOM_DOMAIN_TEMPLATE)"
	aws cloudformation validate-template --template-body file://$(EVENTS_WEBHOOKS_TEMPLATE) >/dev/null && echo "validate-template ok: $(EVENTS_WEBHOOKS_TEMPLATE)"
	aws cloudformation validate-template --template-body file://$(EFS_VOLUMES_TEMPLATE) >/dev/null && echo "validate-template ok: $(EFS_VOLUMES_TEMPLATE)"
	tools="$$(mktemp -d)" && trap 'rm -rf "$$tools"' EXIT && \
	  $(call hashed-tool,cfn-lint,"$$tools") && \
	  "$$tools/bin/cfn-lint" --version && \
	  "$$tools/bin/cfn-lint" -- $(EGRESS_TEMPLATE) $(CI_OIDC_TEMPLATE) $(IAM_TEMPLATE) $(SECRETS_TEMPLATE) $(METADATA_INDEX_TEMPLATE) $(CUSTOM_DOMAIN_TEMPLATE) $(EVENTS_WEBHOOKS_TEMPLATE) $(EFS_VOLUMES_TEMPLATE)

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

# Entorno local con Docker + Floci (docs/site/docs/guias/probar-en-local.md):
# sin AWS ni credenciales. `local-guest-context` deja en LOCAL_GUEST_CONTEXT
# el Dockerfile de producto, el sidecar y un `rayd`: el de LOCAL_RAYD_BIN si
# se pasa (p. ej. `LOCAL_RAYD_BIN=$(RAYD_BIN)` tras `make build`) o, si no,
# uno compilado dentro de Docker con dev/local/rayd/Dockerfile (arm64).
# Lleva el THIRD_PARTY_LICENSES.md de `make licenses` si existe y, si no, un
# marcador: el guest local no se redistribuye, y así levantarlo no pide
# cargo-about en el host (sólo ejercita el COPY del Dockerfile).
LOCAL_DIR     := dev/local
LOCAL_COMPOSE := docker compose -f $(LOCAL_DIR)/compose.yaml
LOCAL_GUEST_CONTEXT := $(LOCAL_DIR)/.guest-context
LOCAL_RAYD_IMAGE := rayito-local-rayd:dev
LOCAL_RAYD_BIN ?=
# Jobs de cargo dentro de dev/local/rayd/Dockerfile (~1 GiB por job al enlazar;
# aws-sdk-s3 solo pasa de 2 GiB, así que esa vía pide ~6 GiB en la VM de Docker).
LOCAL_CARGO_JOBS ?= 2
LOCAL_E2E_ARGS ?=

local-guest-context:
	rm -rf $(LOCAL_GUEST_CONTEXT) && mkdir -p $(LOCAL_GUEST_CONTEXT)
	@if [ -n "$(LOCAL_RAYD_BIN)" ]; then \
	  echo "local-guest-context: rayd de $(LOCAL_RAYD_BIN)"; \
	  cp "$(LOCAL_RAYD_BIN)" $(LOCAL_GUEST_CONTEXT)/rayd; \
	else \
	  docker build -f $(LOCAL_DIR)/rayd/Dockerfile --build-arg CARGO_JOBS=$(LOCAL_CARGO_JOBS) -t $(LOCAL_RAYD_IMAGE) . && \
	  id=$$(docker create $(LOCAL_RAYD_IMAGE)) && \
	  docker cp "$$id:/rayd" $(LOCAL_GUEST_CONTEXT)/rayd; status=$$?; \
	  docker rm "$$id" >/dev/null; exit $$status; \
	fi
	python3 scripts/copy_sidecar.py $(SIDECAR) $(LOCAL_GUEST_CONTEXT)/kernel-sidecar
	mkdir -p $(LOCAL_GUEST_CONTEXT)/licenses
	cp LICENSE NOTICE $(LOCAL_GUEST_CONTEXT)/licenses/
	@if [ -f $(THIRD_PARTY_LICENSES) ]; then \
	  cp $(THIRD_PARTY_LICENSES) $(LOCAL_GUEST_CONTEXT)/licenses/; \
	else \
	  echo "local-guest-context: sin $(THIRD_PARTY_LICENSES) (make licenses), el guest local lleva un marcador"; \
	  printf '%s\n' '# Third-party licenses of rayd' '' 'Local development guest, never redistributed: run `make licenses` to generate the real notices.' > $(LOCAL_GUEST_CONTEXT)/licenses/$(THIRD_PARTY_LICENSES); \
	fi
	cp image/Dockerfile $(LOCAL_GUEST_CONTEXT)/Dockerfile

# El punto de montaje de node_modules tiene que existir en el árbol, que el
# runner monta en sólo lectura. `--wait` espera al healthcheck de Floci.
local-up: local-guest-context
	mkdir -p $(TS_CLIENT)/node_modules
	$(LOCAL_COMPOSE) up -d --build --wait
	$(LOCAL_COMPOSE) exec -T runner bash $(LOCAL_DIR)/bootstrap.sh

# El subconjunto `local` de los e2e de los dos SDK contra el guest y Floci.
local-e2e:
	$(LOCAL_COMPOSE) exec -T runner bash $(LOCAL_DIR)/run-e2e.sh $(LOCAL_E2E_ARGS)

local-down:
	$(LOCAL_COMPOSE) down --volumes --remove-orphans

# Wheel + sdist del SDK Python con las comprobaciones de release.yml
# (contenido de la wheel y `twine check`); no publica nada.
wheel:
	cd $(PYTHON_CLIENT) && uv build
	python scripts/check_wheel.py $(PYTHON_CLIENT)/dist/*.whl
	tools="$$(mktemp -d)" && trap 'rm -rf "$$tools"' EXIT && \
	  $(call hashed-tool,twine,"$$tools") && \
	  "$$tools/bin/twine" check $(PYTHON_CLIENT)/dist/*

# Sitio de documentación (mkdocs-material + mkdocstrings) construido en modo
# estricto desde el entorno del cliente Python, sin deploy.
docs:
	cd $(PYTHON_CLIENT) && uv run --group docs mkdocs build -f ../../docs/site/mkdocs.yml --strict --site-dir ../../docs/site/_build

# Ejemplos del sitio: Python (compile + ruff F821 + mypy contra rayito), JSON,
# YAML, reglas de estilo y las órdenes `rayito ...` contra la CLI; después
# TypeScript con tsc contra clients/typescript
# (necesita `pnpm install` allí). Sin AWS.
docs-examples:
	cd $(PYTHON_CLIENT) && uv run --group dev python ../../scripts/check_docs_examples.py --ruff --mypy --cli
	python3 scripts/check_docs_examples.py --typescript

clean:
	cargo clean
	rm -rf image/rayd image/kernel-sidecar $(IMAGE_LICENSES) $(THIRD_PARTY_LICENSES) $(IMAGE_ZIP) $(IMAGE_ZIP_SLIM) $(IMAGE_ZIP_POLY)
