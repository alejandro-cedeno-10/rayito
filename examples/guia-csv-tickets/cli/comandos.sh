#!/usr/bin/env bash
# Comandos de la CLI del artículo, en orden. No es un script para ejecutar de
# una vez: cópialos por bloques y lee lo que imprime cada uno.
# Versión fijada: rayito 0.15.0. Todo lo que se crea queda en tu cuenta.
set -euo pipefail

# --- 0. Instalación (elige una) ------------------------------------------------
uv sync                                    # desde esta carpeta: rayito[cli]==0.15.0
# pip install "rayito[cli]==0.15.0"
# pnpm add rayito@0.15.0                   # el SDK de TypeScript trae su propia CLI

# --- 1. Preparar la cuenta (una vez por cuenta y región) ------------------------
export AWS_PROFILE="<tu-perfil>" AWS_REGION=us-east-1
uv run rayito setup --dry-run              # el plan y el coste; sólo lee
uv run rayito setup                        # bucket, pila de IAM y la imagen rayito-base
export RAYITO_TEMPLATE=rayito-base RAYITO_BUCKET="<el bucket que imprimió setup>"
uv run rayito doctor --template rayito-base --launch   # lanza y termina un sandbox efímero

# --- 2. El primer sandbox con la CLI --------------------------------------------
mkdir -p ~/.rayito && chmod 700 ~/.rayito
uv run rayito sandbox create rayito-base --detach --timeout 1800 \
  --metadata app=tickets-demo --token-file ~/.rayito/demo.token     # imprime microvm-<id>
uv run rayito sandbox exec "microvm-<id>" --token-file ~/.rayito/demo.token -- python3 -c 'print(40 + 2)'
uv run rayito sandbox list --template rayito-base
uv run rayito sandbox kill "microvm-<id>"
rm -f ~/.rayito/demo.token

# --- 3. Template desde la CLI (alternativa a python/06_template.py) -------------
# uv run rayito template build mi_template.py --name tickets-demo --bucket "$RAYITO_BUCKET"   # mi_template.py está en esta carpeta

# --- 4. Preparativos del agente --------------------------------------------------
gh release download rayd-v0.15.0 -R alejandro-cedeno-10/rayito -p rayito-image.zip -p SHA256SUMS
shasum -a 256 -c --ignore-missing SHA256SUMS     # integridad; la autenticidad es la firma de Sigstore
uv run rayito image publish --artifact rayito-image.zip --base-image-version latest \
  --image-name rayito-base-caps --os-capabilities ALL          # usa RAYITO_BUCKET
uv run rayito agent template build --bucket "$RAYITO_BUCKET" --no-deepagents   # imagen rayito-agent

# La clave del modelo: nunca en la línea de comandos. Desde el entorno...
#   export AWS_BEARER_TOKEN_BEDROCK=...   (en tu shell, no en un fichero del repo)
uv run python/08_crear_secreto.py
# ...o firmando una clave de corta duración con tus credenciales:
# uv run python/08_crear_secreto.py --generar

# Si prefieres la AWS CLI: un fichero temporal 0600 que se borra al salir. El subshell
# evita dejar umask 077 en tu shell o pisar un trap EXIT que ya tuvieras.
# (
#   umask 077; tmp=$(mktemp); trap 'rm -f "$tmp"' EXIT
#   printf 'Bearer %s' "$AWS_BEARER_TOKEN_BEDROCK" > "$tmp"
#   aws secretsmanager create-secret --name rayito/bedrock-key --secret-string "file://$tmp"
# )

# --- 5. Forma B: el agente descrito en un fichero ---------------------------------
uv run rayito agent build --file ./agente-triaje --bucket "$RAYITO_BUCKET"
uv run rayito agent run "La factura de octubre me cobra dos veces el mismo plan" --file ./agente-triaje --stream
uv run rayito agent run "Ejecuta ls -la /home/user y dime qué hay" --file ./agente-triaje --stream   # no debería ejecutarlo; compruébalo en los eventos, no en la respuesta
uv run rayito agent deploy ./agente-triaje --dry-run    # el plan y el coste, sin llamar a AWS
uv run rayito agent deploy ./agente-triaje --yes        # flota de 1 VM
uv run rayito agent invoke triaje "No puedo entrar a mi cuenta desde ayer"
uv run rayito agent stop triaje

# --- 6. Limpieza ------------------------------------------------------------------
uv run rayito sandbox list --template rayito-base               # ¿queda algo vivo?
uv run rayito image prune --image-name tickets-demo --dry-run   # conserva al menos una versión
uv run python/99_limpieza_secreto.py                            # borrado con 7 días de recuperación
# aws secretsmanager restore-secret --secret-id rayito/bedrock-key   # si te equivocaste
# Retirar una imagen entera: delete-microvm-image (API de Lambda MicroVMs) y su grupo de logs /rayito/<nombre>.
