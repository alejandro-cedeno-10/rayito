Exact replacement content for `m15-docs-integration` to apply. This change
does not touch `docs/site/docs/e2b-parity.md`, `optional-features.md`,
`SECURITY.md` or `docs/site/docs/security.md` itself (§5 of the M15
architecture: those tables are edited in one place, by
`m15-docs-integration`, from every feature's `docs-delta.md`).

## `docs/site/docs/e2b-parity.md`

Row **#15** (today: "imposible en la plataforma"). `CustomDomain` does not
remove the header requirement for the default (no-domain) path, but it adds
a header-free alternative — replace the row with:

```
| 15 | `network.allow_public_traffic=True` (el valor por defecto de E2B) | divergente (0.6, experimental) | sin `domain=`: imposible, como en 0.5.x (`X-aws-proxy-auth` obligatorio). Con `domain=CustomDomain(...)` (0.6, experimental, pendiente de D3): un puerto expuesto público no necesita cabeceras, a cambio de un hostname bajo tu dominio en vez de uno de la plataforma | [Dominio propio](funciones-opcionales/dominio-propio.md) |
```

Row **#16** (`Sandbox.traffic_access_token`) — no change needed; `domain=`
reuses the same JWE/traffic-token model, it does not replace it. Left as a
note here for `m15-docs-integration` to confirm, not a replacement row.

Row **#110** (today: "fuera por SPEC", dominio propio vía proxy inverso —
D1 of `v06-foundations/design.md` already removed this non-goal from
`SPEC.md` §4 per ADR-017/018, which the custom-domain line also covers via
ADR-024) — replace with:

```
| 110 | dominio propio vía proxy inverso (docs) | experimental (0.6, `m15-custom-domain`, pendiente de D3) | `CustomDomain` despliega una distribución CloudFront con alias comodín y una CloudFront Function de enrutado (ADR-024); `register()`/`unregister()`/`refresh()` gestionan las rutas. `Sandbox.create(domain=)`/`get_host()`/`expose()` siguen sin cablear (seguimiento no bloqueante, ver `design.md` del cambio): hoy se usa `CustomDomain` directamente | [Dominio propio](funciones-opcionales/dominio-propio.md) |
```

## `docs/site/docs/optional-features.md`

Add one row to the `Función | Estado | Opción Python | Opción TypeScript | Por defecto | Qué activa | Recursos / llamadas AWS | Coste aproximado | IAM necesario | Cómo apagarla | Dónde` table (the function-level table, not the shorter "De un vistazo" summary — add a row there too, same content condensed), with anchor `custom-domain`, right after the `otel-sdk` row (keeps the file's existing order: features land in the order their change merged):

```
<a id="custom-domain"></a>

| [Dominio propio](funciones-opcionales/dominio-propio.md) | experimental (0.6, pendiente de D3) | `CustomDomain(...)` | `new CustomDomain({...})` | sin instanciar = sin cliente AWS | Despliega una distribución CloudFront + CloudFront Function + KeyValueStore (`deploy()`); `register()`/`unregister()`/`refresh()` gestionan qué sandbox responde en `{puerto}-{alias}.<tu dominio>` | `cloudformation:*Stack*` para `deploy`/`status`/`destroy`; `cloudfront-keyvaluestore:DescribeKeyValueStore/PutKey/DeleteKey` por ruta | CloudFront ≈ $0,085/GB + $0,0075/10 000 peticiones HTTPS (salida, us-east-1); CloudFront Functions ≈ $0,10 por 1 000 000 de invocaciones (una por petición); KeyValueStore $0 en reposo, ≈ $0,50 por 1 000 000 de lecturas y ≈ $5 por 1 000 000 de llamadas de gestión (PutKey/DeleteKey) — tres líneas, no una cifra combinada ([precios de CloudFront](https://aws.amazon.com/cloudfront/pricing/); cifras de lista de CloudFront Functions/KeyValueStore desde su lanzamiento, por reconfirmar en la etapa de aceptación AWS) | `cloudformation:*Stack*` (llamante); `cloudfront-keyvaluestore:*` sobre el KVS de la pila (llamante) | No instanciar `CustomDomain` / `new CustomDomain(...)`; `destroy()` borra la distribución, la Function y el KVS | `clients/python/src/rayito/_custom_domain/_service.py` / `clients/typescript/src/custom-domain/service.ts` |
```

Also add a row to the short "De un vistazo" table near the top:

```
| [Dominio propio](funciones-opcionales/dominio-propio.md) | experimental | `CustomDomain(...)`: hostname público por sandbox sin cabeceras del proxy | CloudFront + Function + KVS: céntimos por GB/petición, invocación y llamada de gestión del KVS | desplegar/destruir la distribución (`cloudformation:*Stack*`) y escribir rutas (`cloudfront-keyvaluestore:*`) | no instanciar `CustomDomain` |
```

`FUNCTION_ANCHORS` in `scripts/tests/test_optional_features_docs.py` needs
`"custom-domain"` added to its tuple for the anchor/citation test to cover
this row.

## `SECURITY.md`

Add row **T25** (pre-allocated) to "Amenazas y mitigaciones":

```
| T25 | ingreso público | `m15-custom-domain`: una distribución CloudFront con alias comodín enruta por sandbox vía una CloudFront Function que lee un KeyValueStore. Riesgos: la Function confía en una cabecera `x-aws-proxy-*` que el viewer puso él mismo; una ruta sin `traffic_token` queda pública sin querer; una ruta no borrada tras `kill()` sigue respondiendo hasta que caduca el JWE | La Function borra toda cabecera `x-aws-proxy-*` entrante antes de fijar las suyas (nunca confía en lo que trae la petición); `register()` exige `traffic_token` y rechaza la llamada (`InvalidArgumentException`/`InvalidArgumentError`) si falta, salvo que se pase `public=True` explícito — nunca hay una ruta pública por el valor por defecto; comparación del token en tiempo constante; el JWE de una ruta expira igual que cualquier otro (`TOKEN_REFRESH_AFTER_MINUTES`/TTL de `register()`), así que una ruta huérfana deja de servir sola. `unregister()` es responsabilidad de quien gestiona el ciclo de vida del sandbox (pendiente de cablear a `kill()`, ver `design.md` D6 del cambio) | `m15-custom-domain` (pendiente de aceptación en AWS real, D3) |
```

## `docs/site/docs/security.md`

Add a `## Dominio propio (T25)` section (same shape as "Persistencia en S3
(T15)"), summarizing: what the Function trusts and strips, that a route is
public only when registered without a `traffic_token`, and that
`Sandbox`-level lifecycle wiring (routes torn down automatically on
`kill()`) is a named follow-up, not yet built.

## `docs/site/docs/limits.md`

No change: the 0.6 compatibility row already names `domain=`'s
`UnimplementedError` path (added by `v06-foundations`); this change does
not alter that row since `domain=` still raises.

## `cost.md`

No change: it only links to `optional-features.md` for optional-feature
pricing, which this delta already covers.
