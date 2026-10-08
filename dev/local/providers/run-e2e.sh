#!/usr/bin/env bash
# Corre dentro del contenedor `runner`: los tests `local` de los presets de
# proveedor de los dos SDK, contra el upstream falso (`make
# local-providers-e2e`) o, con `smoke`, contra las APIs reales (`make
# local-providers-smoke`, sólo los presets con RAYITO_SMOKE_<PRESET>_SECRET).
# Los argumentos extra van a pytest.
set -uo pipefail
status=0
cd /src/clients/python
if [ "${1:-}" = "smoke" ]; then
  shift
  uv run --no-sync pytest tests/local/test_local_agent_providers_smoke.py -m local -p no:cacheprovider -v "$@" || status=1
  exit "$status"
fi
uv run --no-sync pytest tests/local/test_local_agent_providers_fake.py -m local -p no:cacheprovider -v "$@" || status=1
cd /src/clients/typescript
pnpm exec vitest run --project local --no-file-parallelism tests/local/agent-providers.local.test.ts || status=1
exit "$status"
