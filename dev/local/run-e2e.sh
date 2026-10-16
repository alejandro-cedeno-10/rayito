#!/usr/bin/env bash
# Corre dentro del contenedor `runner` (make local-e2e): el subconjunto
# `local` de los e2e de Python (marker `local`) y de TypeScript (proyecto
# vitest `local`) contra el guest y Floci. Los argumentos extra van a pytest
# (p. ej. `make local-e2e LOCAL_E2E_ARGS="-k commands"`).
#
# El e2e local de `rayito-mcp` por stdio lanza el ejecutable del paquete, que
# hay que construir antes. El árbol se monta en sólo lectura, así que `tsdown`
# escribe en el volumen de `node_modules` (donde Node también encuentra los
# peers opcionales del servidor), sin los `.d.ts`, que ese test no usa.
set -uo pipefail
status=0
cd /src/clients/python
uv run --no-sync pytest tests/local -m local -p no:cacheprovider -v "$@" || status=1
cd /src/clients/typescript
export RAYITO_TS_DIST=/src/clients/typescript/node_modules/.rayito-dist
pnpm exec tsdown --out-dir "$RAYITO_TS_DIST" --no-dts || status=1
pnpm exec vitest run --project local --no-file-parallelism || status=1
exit "$status"
