# docs-delta: m15-events-webhooks

Applied by `m15-docs-integration` only. Exact replacement content for each
shared file; this change does not edit any of them directly.

## `docs/site/docs/e2b-parity.md`, row 107

Replace the existing row 107 with:

```
| 107 | API REST y webhooks de eventos de ciclo de vida (docs) | divergente (0.6, m15-events-webhooks) | `LifecycleEvents`/`AsyncLifecycleEvents` (`deploy`/`status`/`destroy` sobre tu propia pila, `register_webhook`/`list_webhooks`/`delete_webhook`/`get_events`); el shim no añade ningún método (E2B sólo lo expone por REST) — análogo en tu cuenta: eventos firmados HMAC por `rayd` en CloudWatch Logs, verificados y reenviados a tus webhooks por tres Lambdas en tu propia cuenta, nunca un servicio alojado por Rayito | [Eventos y webhooks](funciones-opcionales/eventos-y-webhooks.md) |
```

## `docs/site/docs/optional-features.md`

Add a new row (after the "Índice de metadatos" row, following the table's
existing column order: feature link | status | Python | TypeScript | default
| what it does | AWS calls/resources | approx. cost | IAM | how to turn off
| source files) and an anchor block mirroring `<a id="metadata-index"></a>`'s
shape, titled "Eventos de ciclo de vida y webhooks", linking to
`funciones-opcionales/eventos-y-webhooks.md`:

```
| [Eventos de ciclo de vida y webhooks](#events-webhooks) | parcialmente implementado en 0.6: `LifecycleEvents`/`register_webhook`/`get_events` funcionan hoy; `Sandbox.create(events=...)` sigue en `UnimplementedError` (hueco de integración compartido por las siete opciones 0.6, ADR-020) | `LifecycleEvents().register_webhook(...)`/`get_events(...)`; `events=` en `Sandbox.create()` no implementado todavía | `new LifecycleEvents().registerWebhook(...)`/`getEvents(...)`; `events` en `Sandbox.create()` no implementado todavía | `None` / `undefined` | `rayd` emite `created`/`paused`/`resumed`/`killed` firmados HMAC por stdout; un forwarder Lambda los verifica y guarda (DynamoDB, TTL 7 días); un deliverer entrega a tus webhooks con firma compatible con E2B y un guardián SSRF; un reconciliador (`rate(5 min)`) sintetiza `killed` para sandboxes que ya no aparecen en `ListMicrovms` (requiere que `Sandbox.create(events=...)` esté implementado para que `rayd` reciba su clave: hoy ningún sandbox emite eventos todavía) | Secrets Manager (`GetSecretValue` del secreto del stack y de cada webhook), DynamoDB (`PutItem`/`Query`/`Scan`/`DeleteItem`/stream), 3 Lambdas (forwarder por lote de log, deliverer por lote del stream, reconciliador cada 5 min), EventBridge Scheduler; infraestructura en `infra/events-webhooks.yaml` | ~$0,40/mes el secreto; DynamoDB y Lambda on-demand/por invocación, ~$1,25 por millón de eventos escritos + lecturas de `get_events`; ver [Coste y activación](funciones-opcionales/eventos-y-webhooks.md) | `EventsOperatorPolicy` (lectura del secreto del stack, lectura/escritura de la tabla de eventos y `cloudformation:DescribeStacks` sobre esta pila) para quien llama a `register_webhook`/`get_events`/`events=`, en las credenciales del **llamante** | No pasar `events=` / `events`; `destroy()` borra el secreto (force-delete), la tabla, las tres Lambdas, la suscripción y el scheduler | `clients/python/src/rayito/_lifecycle_events/` / `clients/typescript/src/lifecycle-events/` |
```

## `SECURITY.md`, threat table

Add row T22 (after T21, before T23), following the existing column
shape (id | threat | context | mitigation | since):

```
| T22 | integridad de eventos y entrega de webhooks | M15 (`m15-events-webhooks`): `rayd` emite eventos de ciclo de vida firmados por stdout; un forwarder/deliverer/reconciliador en Lambda los verifica, guarda y entrega a webhooks del cliente. Riesgos: que un sandbox falsifique un evento o reivindique el `sandbox_id` de otro, que una línea falsificada o repetida llegue a un webhook, que la entrega salga por un destino SSRF, que el secreto del stack o el de un webhook se filtre | **Clave nunca compartida con el agente**: `k_sbx = HMAC-SHA256(stack_key, "rayito.events.v1|" + sandbox_id)` lo deriva el SDK; `rayd` sólo recibe `k_sbx` por `ConfigureSandbox`, nunca `stack_key` — un sandbox comprometido sólo puede falsificar o repetir sus propios eventos. **Doble comprobación de identidad**: el forwarder re-deriva `k_sbx` del secreto del stack (Secrets Manager) y exige que el `sandbox_id` del evento coincida con el *log stream* de CloudWatch por el que llegó, además de verificar el MAC en tiempo constante; una línea que falle cualquiera de los dos se descarta sin escribirse. **Escritura idempotente**: `PutItem` condicional por `event_id`; una línea repetida (reintento de CloudWatch Logs) no duplica fila ni entrega. **Entrega**: firma compatible con E2B (`e2b-signature` = base64 sin relleno de `sha256(secreto + payload)`), sólo `https://`, sin redirecciones, guardián SSRF que resuelve, clasifica y conecta a la dirección ya comprobada (nunca una segunda resolución) — rechaza loopback, privada, link-local (incluida la IMDS), multicast, reservada y CGNAT (100.64.0.0/10); hasta 3 reintentos, deduplicados contra la entrega al menos una vez del stream de DynamoDB. **Secretos**: el secreto HMAC del stack y el de cada webhook viven en Secrets Manager, leídos sólo por el forwarder/deliverer (rol propio, mínimo privilegio) y por el SDK (política `EventsOperatorPolicy`, sólo lectura del secreto del stack); ningún valor aparece en logs ni en los eventos mismos. Apagado por defecto (ADR-014): sin `events=`/`LifecycleEvents`, ningún cliente de DynamoDB/Secrets Manager/CloudFormation y ninguna línea de evento | M15 |
```

Mirror the same T22 row (condensed to the site's existing summary style) into
`docs/site/docs/security.md`.

## `docs/site/docs/cost.md`

Add an entry for `events-webhooks` alongside the other M15 components, using
the `CostStatement` fields from `rayito._stacks.components.events_webhooks`
(see that module and `infra/events-webhooks.yaml`'s description for the
exact figures and sources).

## Notes for `m15-docs-integration`

- `docs/site/docs/funciones-opcionales/eventos-y-webhooks.md`'s "Ver también"
  section links to `../optional-features.md` without the `#events-webhooks`
  anchor (added here instead of in that shared file, so `mkdocs build
  --strict` on this branch does not fail on a fragment that does not exist
  yet). Once the anchor lands, change that link back to
  `../optional-features.md#events-webhooks`.

- `scripts/tests/test_optional_features_docs.py`'s `FUNCTION_ANCHORS` tuple
  (still hard-coded — m15-foundations deferred the `optional_features.d/`
  glob conversion) needs `"events-webhooks"` added alongside applying the
  `optional-features.md` row above, in the same commit — the anchor and the
  registry entry must land together or this test fails either way around.
  `clients/typescript/scripts/check-dts-cost-blocks.mjs`'s `COST_DECLARATIONS`
  already has its `LifecycleEvents` entry (added in this change, since that
  check only reads this feature's own compiled `.d.mts`, independent of the
  shared docs page).

- Row 107's old status ("fuera por SPEC") becomes "divergente (0.6, ...)":
  this is a feature change to an existing row, not a new row — the only
  ADDED-vs-MODIFIED nuance this feature's docs-delta carries.
- No change needed to `docs/site/docs/limits.md` (the 0.6 compatibility row
  is foundations') or to `docs/site/docs/referencia/variables-de-entorno.md`
  (no new environment variable: ADR-014 rule 4).
- `docs/site/docs/referencia/errores.md`'s table already has a slot
  reserved for `WebhookException`/`WebhookError` from foundations' stub
  pass; if it is still empty when this is applied, add:
  `| \`WebhookException\` | \`WebhookError\` | DynamoDB/Secrets Manager | registro, listado o borrado de un webhook falló | revisa el nombre del secreto y el formato de la URL (debe ser https://) |`
