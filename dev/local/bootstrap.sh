#!/usr/bin/env bash
# Corre dentro del contenedor `runner` (make local-up): instala las
# dependencias de los dos SDK desde sus lockfiles en los volúmenes del
# entorno (UV_PROJECT_ENVIRONMENT y node_modules), sin tocar los del host.
set -euo pipefail
cd /src/clients/python
uv sync --frozen
cd /src/clients/typescript
pnpm install --frozen-lockfile
