#!/usr/bin/env bash
# Corre dentro del contenedor `runner` (make local-e2e): el subconjunto
# `local` de los e2e de Python (marker `local`) y de TypeScript (proyecto
# vitest `local`) contra el guest y Floci. Los argumentos extra van a pytest
# (p. ej. `make local-e2e LOCAL_E2E_ARGS="-k commands"`).
set -uo pipefail
status=0
cd /src/clients/python
uv run --no-sync pytest tests/local -m local -p no:cacheprovider -v "$@" || status=1
cd /src/clients/typescript
pnpm exec vitest run --project local --no-file-parallelism || status=1
exit "$status"
