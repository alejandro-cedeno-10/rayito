#!/usr/bin/env bash
# Un proxy de LiteLLM delante de Amazon Bedrock para los tests `local` de
# proveedores (clients/python/tests/local/test_local_agent_providers.py,
# ruta `litellm`): `make local-litellm-up` / `make local-litellm-down`,
# después de `make local-up` y `make local-agent-up`.
#
#   up    genera una CA y un certificado de prueba (para `litellm`, un día,
#         en un directorio temporal que se borra al terminar), arranca
#         LiteLLM en la red `egress` del entorno con el alias `litellm` y
#         sin puertos en el host, añade la CA al almacén del sistema del
#         guest (rayd verifica el upstream con él) y reinicia el guest,
#         porque rayd lee ese almacén al arrancar; deja la clave maestra en
#         el tmpfs del runner (RAYITO_LOCAL_LITELLM_KEY_FILE). Ningún
#         sandbox debe estar vivo mientras tanto.
#   down  borra el contenedor, la CA del guest y la clave del runner.
#
# LiteLLM habla con Bedrock con las credenciales temporales de tu sesión
# (session_env.py las pone en el entorno de `docker create`), que sólo
# viajan como variables de entorno del contenedor: ni stdout ni ningún
# fichero las ven. Caducan con tu sesión; vuelve a
# ejecutar `up` entonces. Coste: el de las llamadas a Bedrock que hagan los
# tests (céntimos).
set -euo pipefail

#: LiteLLM v1.104.0 (MIT), fijado por el digest de la manifest list (amd64 y
#: arm64) consultado el 2026-10-07 en ghcr.io/berriai/litellm.
LITELLM_IMAGE="ghcr.io/berriai/litellm@sha256:625981c83410a3ea68eb0697590a57ec1d764d634514d54fa5db0591077ee839"
LITELLM_HOST="litellm"
LITELLM_PORT="4000"
#: Lo que tarda LiteLLM en arrancar en un portátil (medido: 10-100 s).
STARTUP_TIMEOUT_SECONDS=240
CA_ANCHOR="/etc/pki/ca-trust/source/anchors/rayito-local-litellm.crt"
REGION="${RAYITO_E2E_BEDROCK_REGION:-us-east-1}"

workdir=""
here="$(cd "$(dirname "$0")" && pwd)"
local_dir="$(dirname "$here")"
project="${COMPOSE_PROJECT_NAME:-rayito-local}"
container="${project}-litellm"
compose=(docker compose -f "$local_dir/compose.yaml" -f "$here/compose.yaml")
py=(uv run --project "$local_dir/../../clients/python" python)

config() {
  cat <<EOF
model_list:
  - model_name: gpt-oss-120b
    litellm_params:
      model: bedrock/openai.gpt-oss-120b-1:0
      aws_region_name: ${REGION}
  - model_name: claude-haiku
    litellm_params:
      model: bedrock/us.anthropic.claude-haiku-4-5-20251001-v1:0
      aws_region_name: ${REGION}
litellm_settings:
  drop_params: true
general_settings:
  master_key: os.environ/LITELLM_MASTER_KEY
EOF
}

certificates() {
  local dir="$1"
  openssl req -x509 -newkey ec -pkeyopt ec_paramgen_curve:P-256 -nodes \
    -keyout "$dir/ca.key" -out "$dir/ca.crt" -days 1 -subj "/CN=rayito local LiteLLM CA" \
    -addext "basicConstraints=critical,CA:TRUE" -addext "keyUsage=critical,keyCertSign" 2>/dev/null
  openssl req -newkey ec -pkeyopt ec_paramgen_curve:P-256 -nodes \
    -keyout "$dir/serve/server.key" -out "$dir/server.csr" -subj "/CN=${LITELLM_HOST}" 2>/dev/null
  printf 'subjectAltName=DNS:%s\nextendedKeyUsage=serverAuth\n' "$LITELLM_HOST" >"$dir/ext.cnf"
  openssl x509 -req -in "$dir/server.csr" -CA "$dir/ca.crt" -CAkey "$dir/ca.key" \
    -CAcreateserial -out "$dir/serve/server.crt" -days 1 -extfile "$dir/ext.cnf" 2>/dev/null
  chmod 0644 "$dir/serve/server.key"
}

up() {
  down >/dev/null 2>&1 || true
  workdir="$(mktemp -d)"
  trap 'rm -rf "$workdir"' EXIT
  local dir="$workdir"
  mkdir "$dir/serve"
  certificates "$dir"
  config >"$dir/serve/config.yaml"
  LITELLM_MASTER_KEY="sk-$(openssl rand -hex 24)"
  export LITELLM_MASTER_KEY
  "${py[@]}" "$here/session_env.py" docker create --name "$container" --network "${project}_egress" --network-alias "$LITELLM_HOST" \
    --cap-drop ALL --security-opt no-new-privileges:true --memory 1g \
    -e AWS_ACCESS_KEY_ID -e AWS_SECRET_ACCESS_KEY -e AWS_SESSION_TOKEN -e AWS_REGION="$REGION" \
    -e LITELLM_MASTER_KEY -e LITELLM_LOCAL_MODEL_COST_MAP=True -e LITELLM_TELEMETRY=False \
    "$LITELLM_IMAGE" --config /cfg/config.yaml --port "$LITELLM_PORT" \
    --ssl_keyfile_path /cfg/server.key --ssl_certfile_path /cfg/server.crt >/dev/null
  docker cp "$dir/serve/." "$container:/cfg" >/dev/null
  docker start "$container" >/dev/null
  "${compose[@]}" exec -T -u root guest sh -c "cat > $CA_ANCHOR && update-ca-trust" <"$dir/ca.crt"
  "${compose[@]}" restart guest >/dev/null
  "${compose[@]}" up -d --wait guest >/dev/null
  printf '%s' "$LITELLM_MASTER_KEY" |
    "${compose[@]}" exec -T runner sh -c 'umask 077 && cat > "$RAYITO_LOCAL_LITELLM_KEY_FILE"'
  local waited=0
  until "${compose[@]}" exec -T guest curl -sf -o /dev/null \
    "https://${LITELLM_HOST}:${LITELLM_PORT}/health/liveliness"; do
    if [ "$waited" -ge "$STARTUP_TIMEOUT_SECONDS" ]; then
      echo "litellm: no arrancó en ${STARTUP_TIMEOUT_SECONDS} s (docker logs $container)" >&2
      return 1
    fi
    sleep 5
    waited=$((waited + 5))
  done
  echo "litellm: listo en https://${LITELLM_HOST}:${LITELLM_PORT} (${waited} s)"
}

down() {
  docker rm -f "$container" >/dev/null 2>&1 || true
  "${compose[@]}" exec -T -u root guest sh -c "rm -f $CA_ANCHOR && update-ca-trust" || true
  "${compose[@]}" exec -T runner sh -c 'rm -f "$RAYITO_LOCAL_LITELLM_KEY_FILE"' || true
}

case "${1:-}" in
  up) up ;;
  down) down ;;
  *)
    echo "uso: $0 up|down" >&2
    exit 2
    ;;
esac
