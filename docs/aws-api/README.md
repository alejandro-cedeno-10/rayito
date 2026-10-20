# docs/aws-api — apéndice crudo de la API de Lambda MicroVMs

Salida **sin editar** capturada el 2026-09-14 con `aws-cli/2.36.23`. Es la
fuente que `AWS_API_NOTES.md` cita; la regla 1 de `CLAUDE.md` (no inventar
parámetros) considera este directorio parte de `AWS_API_NOTES.md`.

| Fichero | Origen |
|---|---|
| `cli/00_help.txt` | `aws lambda-microvms help` |
| `cli/<comando>.txt` | `aws lambda-microvms <comando> help`, uno por comando |
| `service-2.json` | Modelo botocore `lambda-microvms/2025-09-09/service-2.json` empaquetado en la CLI |
| `paginators-1.json` | Paginadores del mismo modelo |
| `model_summary.md` | Resumen generado del modelo: cada operación con shapes de entrada/salida, enums y límites |
| `bedrock-agentcore-control/2023-06-05/*.json.gz` | Modelo y reglas de endpoint de `bedrock-agentcore-control` copiados sin cambios de botocore 1.43.103; los empaqueta `scripts/gen_stack_assets.py` en el manejador de los constructos CDK (ADR-032, `AWS_API_NOTES.md` Q183) |

Para refrescar tras actualizar la CLI:

```bash
for c in $(aws lambda-microvms help | grep -E '^\* ' | sed 's/^\* //' | grep -v '^help$'); do
  aws lambda-microvms $c help > docs/aws-api/cli/$c.txt 2>&1
done
```
