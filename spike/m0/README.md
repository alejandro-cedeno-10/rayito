# spike/m0 — validación de la plataforma

Material del hito M0 (`MILESTONES.md`). No es código de producto: no hay nada en
`src/`. Su único objetivo es convertir las preguntas de `AWS_API_NOTES.md` §16
en medidas reales. Ejecutado el 2026-09-15 contra una cuenta de pruebas
(us-east-1); resultados en `M0_RESULTS.md`. El crudo va a `out/`, que se
regenera en local con `run_m0.py` y nunca se versiona (lleva el ID de cuenta,
ARNs, endpoints e IDs de MicroVM de quien lo ejecuta).

| Fichero | Qué es |
|---|---|
| `image/Dockerfile`, `image/probe.py` | Imagen "probe": servidor HTTP stdlib con los hooks en `:9000` y endpoints de medición en `:8080` (IMDS, reloj, procesos, sockets, ipykernel, stream, ancho de banda, `/echo` de cabeceras). Lambda la construye desde el zip; no hace falta Docker ni ARM64 local |
| `iam.yaml` | CloudFormation: build role (`s3:GetObject` sólo sobre `rayito/*`), execution role (solo logs) y managed policy de mínimo privilegio para el caller |
| `run_m0.py` | Runbook paso a paso en Python (boto3 + requests, sin jq/zip/bc): `iam → image → run → probe → suspend → resume → streams → fleet → cleanup`. Escribe crudo en `out/` y una línea por pregunta en `out/results.jsonl`; el estado entre pasos (`image_arn`, `microvm_id`, `host`, token) va en `out/state.json`; todo `out/` está ignorado por git |
| `M0_RESULTS.md` | Tabla con lo medido |
| `test_probe_local.py` | Test local del probe (Windows/Linux) sin AWS: arranca `probe.py`, dispara los hooks y valida los endpoints |

## Ejecutar

```bash
aws sso login --profile <tu-perfil>
export AWS_PROFILE=<tu-perfil> AWS_REGION=us-east-1
# obligatorio: un bucket existente en AWS_REGION para el zip de la sonda
export RAYITO_BUCKET=<tu-bucket>
uv run --with "boto3>=1.43.82" --with requests --with "httpx[http2]" python spike/m0/run_m0.py all   # ≈ 35 min, < $2
```

Pasos sueltos: `... run_m0.py image`, `... run_m0.py run`, etc. `all` ejecuta
`cleanup` siempre, incluso si un paso falla. `httpx[http2]` sólo hace falta para
la pregunta 17 (ningún `curl` de Windows trae HTTP/2); sin él el paso la marca
como omitida.

Variables opcionales: `RAYITO_IMAGE_NAME` (`rayito-m0-probe`), `RAYITO_STACK_NAME`
(`rayito-m0-iam`), `RAYITO_LOG_GROUP` (`/rayito/<imagen>`), `RAYITO_SUSPEND_SECONDS`
(300).

Requisitos en la máquina: `uv` (Python 3.12) y credenciales AWS. Nada más.

## Test local (sin AWS)

```bash
uv run --with jupyter_client --with ipykernel python spike/m0/test_probe_local.py
```

Valida que los seis hooks responden en `/aws/lambda-microvms/runtime/v1/*`, que
`/run` captura `runHookPayload`, y que los endpoints de kernel/procesos/sockets
funcionan. Lo que no se puede probar localmente (IMDS, capabilities, proxy) se
salta con un aviso.

## Lo que M0 NO cubre

- gRPC real a través del proxy (`X-aws-proxy-force-h2`, trailers, streams bidi):
  necesita un binario tonic ARM64 → primer test de M1. M0 sólo mide qué ve una
  app HTTP/1.1 con y sin `X-aws-proxy-force-h2` (pregunta 17).
- `/suspend` devolviendo 500 (pregunta 10): requiere una segunda versión de la
  imagen con `PROBE_FAIL_SUSPEND=1`; no ejecutado.
- Coste real (preguntas 8 y 22): Cost Explorer tarda ~24 h; revisar al día siguiente.
