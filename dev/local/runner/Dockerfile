# Contenedor `runner` del entorno local (docs/site/docs/guias/probar-en-local.md):
# las toolchains de los dos SDK (Python con uv, Node con pnpm) para correr
# `make local-e2e`. No contiene código del repositorio ni secretos: el árbol
# se monta en /src en tiempo de ejecución, y el entorno virtual y los
# node_modules viven en volúmenes con nombre (los del host son de macOS).
#
# Cada imagen va fijada por digest de la manifest list (consultado
# 2026-10-04 contra el registro); el tag delante del `@sha256:` es solo para
# leerlo. Sin sintaxis de BuildKit: construye también con el builder clásico.
FROM node:22.23.2-bookworm-slim@sha256:48e4b67d85f87bd551df43704e24d252f56cc5f8e9718841aace50f19948f0f9 AS node
FROM ghcr.io/astral-sh/uv:0.12.18@sha256:3adc3706091ce7c2fe595e669628caedd6d951551b92b258b7e7dbe06d9440bc AS uv
FROM python:3.12.13-slim-bookworm@sha256:4766d8b510c428e595d74b9cc5bbb2fae8e26316fffb4adc89908d79aacd58a2

# Node trae corepack, que activa el pnpm de `packageManager`
# (clients/typescript/package.json) y verifica su firma contra las claves
# del registro de npm. Se prepara aquí para no descargar nada al correr.
COPY --from=node /usr/local/bin/node /usr/local/bin/node
COPY --from=node /usr/local/lib/node_modules/corepack /usr/local/lib/node_modules/corepack
COPY --from=uv /uv /uvx /usr/local/bin/

ENV COREPACK_HOME=/opt/corepack \
    COREPACK_ENABLE_DOWNLOAD_PROMPT=0 \
    UV_PYTHON_DOWNLOADS=never \
    UV_PYTHON=/usr/local/bin/python3.12 \
    UV_PROJECT_ENVIRONMENT=/opt/rayito/venv \
    UV_CACHE_DIR=/opt/rayito/cache/uv \
    UV_LINK_MODE=copy \
    npm_config_store_dir=/opt/rayito/cache/pnpm \
    PYTHONDONTWRITEBYTECODE=1

# `runner` es quien corre los tests y, con ellos, el plano de control local
# que llama a los hooks del guest como lo haría la plataforma. Por eso su uid
# queda fuera del rango del sandbox (1000-65535): `rayd` rechaza un `/run`
# (`sandbox_origin`) y un `/terminate` (`peer_refused`) cuyo socket es de un
# uid del sandbox (`rayd_core::hook_peer`, SECURITY.md T2), y el agente de la
# plataforma posee los suyos como uids 991-994 (AWS_API_NOTES.md Q48). Debe
# coincidir con el `uid=` del tmpfs de /home/runner en compose.yaml.
# Los directorios de los volúmenes se crean aquí con su dueño: un volumen con
# nombre copia el dueño del directorio de la imagen la primera vez que se
# monta.
ARG RUNNER_UID=993
RUN ln -s ../lib/node_modules/corepack/dist/corepack.js /usr/local/bin/corepack \
    && corepack enable pnpm \
    && corepack prepare pnpm@9.15.4 --activate \
    && chmod -R a+rX /opt/corepack \
    && groupadd -g "$RUNNER_UID" runner \
    && useradd -m -u "$RUNNER_UID" -g runner -s /bin/bash runner \
    && mkdir -p /opt/rayito/venv /opt/rayito/cache /src/clients/typescript/node_modules \
    && chown -R runner:runner /opt/rayito /src \
    && node --version && pnpm --version && uv --version

USER runner
WORKDIR /src

# El contenedor solo sostiene el espacio de red que comparte con el guest
# (`network_mode: service:runner` en compose.yaml); los tests entran con
# `docker compose exec`. `init: true` en compose le da un PID 1 que reenvía
# las señales.
CMD ["sleep", "infinity"]
