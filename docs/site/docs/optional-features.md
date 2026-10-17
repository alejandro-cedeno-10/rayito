# Funciones opcionales y su coste

Rayito nunca cobra por sorpresa. Sin opciones, un sandbox sólo cuesta el
propio MicroVM: el SDK no crea ningún otro recurso de AWS ni hace llamadas
de pago. Cada función de esta página está **apagada por defecto** y se
enciende con una **opción explícita** del SDK, nunca con una variable de
entorno ni un fichero de configuración. Lo que necesitan (una tabla, unas
políticas IAM) lo despliegas tú en tu cuenta con una plantilla de
CloudFormation; Rayito no hospeda ningún servidor.

## De un vistazo

| Función | Por defecto | Qué activa | Coste | IAM | Cómo apagarla |
|---|---|---|---|---|---|
| [Inyección de secretos](secrets.md) | apagada | `secrets=` / `secrets`: el valor de un secreto de Secrets Manager llega como variable de entorno a un comando, terminal o celda | $0,40 por secreto y mes + $0,05 por 10 000 lecturas (una por secreto cada 5 min con la caché por defecto) | leer los secretos `rayito/*` (`RayitoSecretsReader`, pila `secrets-access`) | no pasar `secrets=` |
| [Gestión de secretos (CRUD)](secrets.md) | apagada | `SecretStore(...)`: crear, listar, actualizar y borrar secretos (también `Secret` del shim de E2B) | $0,40 por secreto y mes **hasta que lo borras** + $0,05 por 10 000 llamadas | administrar los secretos `rayito/*` (`RayitoSecretsAdmin`, pila `secrets-access`) | no instanciar `SecretStore` y `destroy()` los secretos creados |
| [Índice de metadatos](funciones-opcionales/indice-de-metadatos.md) | apagado | `index=DynamoDbIndex(...)`: filtrar `list()` por metadatos también sobre sandboxes en pausa | ≈ $0,03/mes para 10 000 sandboxes; tabla vacía, $0 | escribir y leer en tu tabla (`RayitoIndexWriter`, `RayitoIndexReader`, pila `metadata-index`) | no pasar `index=`; `rayito stack destroy metadata-index` |
| [Trazas OpenTelemetry](funciones-opcionales/opentelemetry.md) | apagadas | `tracer_provider=` / `tracerProvider`: spans `rayito.*` de tus llamadas | $0 desde Rayito; lo que cueste tu exportador | ninguno propio | no pasar `tracer_provider=` |
| [Montajes S3](funciones-opcionales/montajes-s3.md) | apagados | `mounts=` / `mounts`: un bucket S3 como carpeta del sandbox (`rayito-base-caps`) | $0 propio; las peticiones de S3 del bucket | `RayitoS3MountAccess` en el execution role (pila `s3-mounts`) | no pasar `mounts=` |
| [Tamaños](funciones-opcionales/tamanos.md) | apagados | `size=` / `size`: lanzar la imagen de otro tamaño (512mb a 8gb) | más $/h cuanto mayor el tamaño; snapshot por imagen publicada | ninguno adicional (la pila opcional `sizes-guard` restringe qué tamaños se lanzan) | no pasar `size=` |
| [Eventos y webhooks](funciones-opcionales/eventos-y-webhooks.md) | apagados | `events=` / `events`: eventos de ciclo de vida guardados y entregados a tus webhooks | ≈ $0,40/mes la pila en reposo; el resto por uso | `EventsLauncherPolicy`, `EventsReaderPolicy` o `EventsWebhookAdminPolicy` según la tarea (pila `events-webhooks`) | no pasar `events=`; `rayito events destroy` |
| [Exportación OTLP](funciones-opcionales/exportacion-otlp.md) | apagada | `telemetry=` / `telemetry`: métricas del sandbox por OTLP a CloudWatch | ≈ $0,00002 por sandbox-hora | `RayitoOtlpExport` en el execution role (pila `otlp-export`) | no pasar `telemetry=` |
| [Pasarela de secretos](funciones-opcionales/pasarela-de-secretos.md) | apagada | `gateways=` / `gateways`: usar un secreto desde el sandbox sin poder leerlo (con presets para el modelo de un agente: `bedrock_gateway`, `anthropic_gateway`, `openai_compatible_gateway` y los de [Proveedores del agente](guias/agente-proveedores.md)) | $0,05 por 10 000 lecturas + el secreto | leer los secretos (`RayitoSecretsReader`) | no pasar `gateways=` |
| [Templates](funciones-opcionales/templates.md) | apagados | `Template.build()`: construir una imagen desde el DSL | ≈ $0,04/semana por versión de imagen | `RayitoTemplateBuilder` (pila `templates`) | no llamar a `Template.build()` |
| [Templates de agente](funciones-opcionales/templates-de-agente.md) (desde 0.8.0) | apagado | `AgentTemplate(...).build()`: imagen con OpenCode/deepagents y sus pines | ≈ $0,057/semana por versión de imagen (≈ 3,1 GB; ver [Precios](cost.md#plaza-de-pool-de-agente-c)) | `RayitoTemplateBuilder` (pila `templates`) | no construir el template |
| [Agente en el sandbox](guias/agente-en-el-sandbox.md) (desde 0.8.0) | apagado | `sbx.agent.run()`/`.stream()`/`.prepare()`: un agente de código corriendo dentro del sandbox, con su modelo por la pasarela | $0 propio del SDK; el modelo por tokens (ver [coste de un agente](cost.md#coste-de-un-agente-vm-frente-a-modelo)) y lo que ya cuesta la pasarela | el de la pasarela de secretos elegida | no llamar a `sbx.agent` |
| [Despliega tu agente](guias/despliega-tu-agente.md) (desde 0.14.0) | apagado; publicado en 0.14.0, aceptado en AWS real el 2026-10-17 | `AgentProject.build()`/`.run()`/`.deploy()` y `rayito agent build`, `run` y `deploy`: un agente de Strands, Hermes, LangGraph, deepagents, OpenCode o propio como imagen, ejecución puntual o servicio HTTP (`export()` no llama a AWS) | una versión de imagen (≈ $0,056/semana con `rayito-agent`) y, si lo despliegas, la VM mientras viva (≈ $0,126/h; ver [Precios](cost.md#coste-de-un-agente-desplegado)) + el modelo por tokens | `RayitoTemplateBuilder` (pila `templates`) y el de la pasarela de secretos elegida | no llamar a `build()`/`run()`/`deploy()`; `rayito agent stop` |
| [Varias sesiones y varias VMs](guias/flota-de-agentes.md) (desde 0.14.0) | apagadas; publicadas en 0.14.0, aceptadas en AWS real el 2026-10-17 | `deploy_agent()`/`deployAgent()` y `rayito agent deploy`: un agente que ya tienes, detectado sin tocar su código, como una flota (`ScalingOptions`: `min_vms`, `max_vms`, `warm_vms`, sesiones compartidas o dedicadas) con cola; `--dry-run` y `detect` no llaman a AWS | por VM en marcha ≈ $0,126/h con 2 GB, una versión de imagen ≈ $0,056/semana y las plazas aparcadas sólo el snapshot (ver [Precios](cost.md#coste-de-un-agente-desplegado)) + el modelo por tokens | los de `Sandbox.create()`/`list()`/`kill()` y `AgentTemplate.build()` (`RayitoSandboxRunner`, `RayitoTemplateBuilder`) | no llamar a `deploy_agent()`; `fleet.stop()` o `rayito agent stop <nombre>` |
| Crear el secreto del modelo (desde 0.14.0) | apagada | `--create-secret` / `create_secret_from_env=True`: guarda la clave de la variable estándar del proveedor en Secrets Manager al desplegar | $0,40 por secreto y mes hasta que lo borras | `secretsmanager:CreateSecret` | no pasar la opción; `SecretStore.destroy()` |
| Publicar la imagen base al desplegar (desde 0.14.0) | apagada | `--base-artifact ZIP --yes` / `base_publisher=`: publica `rayito-base-caps` si falta | ≈ $0,056/semana por versión de imagen | los de `rayito image publish` | no pasar la opción; `rayito image prune` |
| [Pool: calentamiento (`warmup`)](pool.md#calentamiento-warmup) (desde 0.8.0) | apagado | `PoolConfig(warmup=agent_pool_warmup(...))`: precalentar el runtime del agente antes de aparcar cada plaza | ≈ $0,64/plaza/mes con `warmup` (frente a $0,60 de una plaza base; ver [Precios](cost.md#plaza-de-pool-de-agente-c)) | el de la imagen/pool que ya uses | no pasar `warmup=` |
| [Volúmenes EFS](funciones-opcionales/volumenes-efs.md) (experimental) | apagados | `volumes=` / `volumes`: un sistema de ficheros EFS compartido en vivo entre sandboxes (imagen `rayito-base-caps-efs`); `VolumeStore`/`EfsVolumes` para los volúmenes y la pila | $0 vacío; $0,30/GB-mes y $0,03/$0,06 por GB leído/escrito (Elastic); la imagen ocupa ≈ 198 MB más | `CallerPolicyArn` de la pila `efs-volumes` en el execution role; `DescribeMountTargets` y el CRUD de access points en el llamante | no pasar `volumes=`; `EfsVolumes.destroy(delete_file_system=True)` |
| [Dominio propio](funciones-opcionales/dominio-propio.md) (experimental) | apagado | `CustomDomain(...)`: una URL HTTPS bajo tu dominio para un puerto del sandbox, sin cabeceras del proxy (distribución CloudFront en tu cuenta, pila `custom-domain`); sin verificar aún de punta a punta en AWS real | $0 en reposo; ~$0,085/GB + $0,0075/10 000 peticiones HTTPS, más la Function y una lectura del KeyValueStore por petición | CloudFormation y CloudFront para la pila; `cloudfront-keyvaluestore:DescribeKeyValueStore/PutKey/DeleteKey` para las rutas | no instanciar `CustomDomain`; `rayito domain destroy` |
| [Panel de gestión (SDK y panel local)](funciones-opcionales/panel-de-gestion.md) | apagado | `Quotas`, `UsageCost`, `Fleet` y `rayito dashboard` (en las dos CLI): cuotas y memoria asignada, coste facturado y estimado, sandboxes, imágenes y acciones masivas (sólo lectura salvo `allow_actions=True` / `--allow-actions`); el panel local sirve una página en `127.0.0.1` | $0 las lecturas, `estimate()` y `Quotas.report()`; de pago: `billed()` $0,01 por petición a Cost Explorer (caché de 24 h por instancia, ≈ $0,30/mes con una instancia de larga vida; en el panel sólo con `--cost-explorer`), `events()` lecturas de DynamoDB (sólo con `--events-stack`); `resume` vuelve a facturar cómputo | `servicequotas:ListServiceQuotas`, lecturas de Lambda MicroVMs, `ce:GetCostAndUsage` sólo con `billed()` / `--cost-explorer` | no llamar a sus métodos; cerrar el panel con Ctrl+C |
| [Panel en tu cuenta (pila `dashboard`)](funciones-opcionales/panel-en-tu-cuenta.md) | apagado | `rayito stack deploy dashboard` / `OptionalStacks().deploy("dashboard")`: la misma página y API que el panel local, servidas desde una Lambda con Function URL y Cognito, con rol de lectura y rol de acciones separados y auditoría sólo de añadir; tema propio por `ThemeJson` o SSM | ≈ $0 en reposo (Lambda, DynamoDB bajo demanda y Cognito sólo por uso; la Function URL es pública y el tráfico sin autenticar factura invocaciones, acótalo con `ReservedConcurrency`); Cost Explorer $0,01 por consulta sólo con `EnableCostExplorer=true` (peor caso ≈ $6/mes); un parámetro SSM `Advanced` de tema o logo, $0,05/mes más interacciones | los permisos de la pila (`CAPABILITY_IAM`); la función sólo lee MicroVMs y cuotas, y actúa únicamente con el rol de acciones de `AllowActions=true` | `rayito stack destroy dashboard` |
| [Dashboard de CloudWatch](funciones-opcionales/panel-extras.md#dashboard-de-cloudwatch) | apagado | `rayito dashboard cloudwatch` imprime el JSON de un dashboard (Logs Insights sobre `/rayito/*`, métricas OTLP y uso de cuota) sin llamar a AWS; la pila `dashboard-cloudwatch` lo despliega | $0 con menos de 4 dashboards, $3/mes por dashboard desde el cuarto; Logs Insights factura los GB escaneados al abrir o refrescar | quien despliega, `cloudwatch:PutDashboard`; quien mira, `logs:StartQuery` | `rayito stack destroy dashboard-cloudwatch` |
| [Varias cuentas en el panel](funciones-opcionales/panel-extras.md#varias-cuentas) | apagado | `rayito dashboard --account NOMBRE=ARN_DEL_ROL`: ver otras cuentas, de sólo lectura, con un rol que se asume; la pila `dashboard-read-role` crea ese rol en cada cuenta destino | $0 (IAM y STS no facturan) | `sts:AssumeRole` sobre los roles destino; cada rol, cinco lecturas | no pasar `--account`; `rayito stack destroy dashboard-read-role` |
| [Avisos de cuota](funciones-opcionales/panel-extras.md#avisos-de-cuota) | apagado | `rayito stack deploy quota-alerts`: una Lambda programada publica en SNS cuando la memoria asignada cruza un umbral de la cuota (un aviso por cruce) y publica el uso como métrica | ≈ $0,30/mes (una métrica personalizada); el resto en capas gratuitas | `CAPABILITY_IAM`; la función lee MicroVMs y cuotas y escribe en su tema, su fila y su espacio de métricas | `rayito stack destroy quota-alerts` |
| [Proxy local](funciones-opcionales/proxy-local.md) (`rayito sandbox proxy`) | — (sólo CLI, en Python y en TypeScript) | sirve un puerto del sandbox en `127.0.0.1` | $0: usa llamadas gratuitas de Lambda | el que ya da `infra/iam.yaml` (`SandboxLauncherPolicy`) | `Ctrl+C` |
| [Servidor MCP](mcp.md) (`rayito-mcp`) | apagado | el ejecutable (extra `mcp` de Python o peers opcionales `@modelcontextprotocol/server` y `zod` de TypeScript): un sandbox por proceso del servidor, creado en la primera herramienta que lo necesita y destruido al cerrar; con `--http`, sólo loopback y sin autenticación | un MicroVM en ejecución mientras se usa y un snapshot por ciclo de suspensión; nada vive más allá de `RAYITO_MCP_TIMEOUT_SECONDS` | `SandboxLauncherPolicy` (`infra/iam.yaml`) | cerrar el servidor desde el host MCP |

Cada nombre lleva a la guía completa de la función; debajo hay un
[ejemplo corto](#ejemplos) de las más usadas. Son cifras aproximadas de
us-east-1: las exactas, con su fecha de consulta y el enlace a la página de
precios de AWS, están en [Funciones con coste AWS](#funciones-con-coste-aws)
y en la guía de cada función. El precio de los MicroVMs, del pool y de un
agente está en [Precios](cost.md).

!!! info "La infraestructura la despliegas tú: `rayito stack`"
    Las funciones que necesitan algo en tu cuenta (una tabla, unas
    políticas IAM, unas Lambdas) lo traen como componente de
    [`rayito stack`](funciones-opcionales/pilas-opcionales.md):
    `rayito stack deploy <componente>` imprime el bloque "Coste y
    activación" y pide confirmación, `rayito stack status` dice qué hay
    desplegado y `rayito stack destroy` lo borra. Desde 0.6.1, redesplegar
    conserva los parámetros que no repites. En el SDK, `OptionalStacks`.

Los nueve componentes de `rayito stack`, y qué función de la tabla de
arriba usa cada uno. Ninguno se despliega solo:

| Componente | Para qué función | Qué crea | Coste en reposo |
|---|---|---|---|
| `secrets-access` | [Inyección y gestión de secretos](secrets.md), [pasarela de secretos](funciones-opcionales/pasarela-de-secretos.md) | 2 políticas IAM | $0 |
| `metadata-index` | [Índice de metadatos](funciones-opcionales/indice-de-metadatos.md) | tabla DynamoDB on-demand + 2 políticas IAM | $0 con la tabla vacía |
| `s3-mounts` | [Montajes S3](funciones-opcionales/montajes-s3.md) | 1 política IAM | $0 |
| `sizes-guard` | [Tamaños](funciones-opcionales/tamanos.md) (opcional) | 1 política IAM | $0 |
| `events-webhooks` | [Eventos y webhooks](funciones-opcionales/eventos-y-webhooks.md) (también `rayito events deploy`) | secreto, tabla, 3 Lambdas, colas, scheduler y políticas | ≈ $0,40/mes |
| `otlp-export` | [Exportación OTLP](funciones-opcionales/exportacion-otlp.md) con el execution role | 1 política IAM | $0 |
| `templates` | [Templates](funciones-opcionales/templates.md) y [templates de agente](funciones-opcionales/templates-de-agente.md) | 1 política IAM | $0 |
| `efs-volumes` (experimental) | [Volúmenes EFS](funciones-opcionales/volumenes-efs-vpc.md) | sistema de ficheros EFS, mount targets, grupos de seguridad, conector, rol y política, en tu VPC | $0 con el sistema de ficheros vacío |
| `custom-domain` (experimental) | [Dominio propio](funciones-opcionales/dominio-propio.md) (también `rayito domain deploy`) | distribución CloudFront, Function y KeyValueStore | $0 sin tráfico |

Parámetros de cada uno: [Pilas opcionales](funciones-opcionales/pilas-opcionales.md#parametros-de-cada-componente).

!!! note "Experimentales"
    Volúmenes EFS y dominio propio, publicados en
    [0.7.0](novedades/0.7.0.md), son **experimentales**: puedes usarlos,
    pero su API puede cambiar en una minor, y el dominio propio todavía no
    se ha verificado de punta a punta en AWS real (ver el aviso de
    [su página](funciones-opcionales/dominio-propio.md)). `domain=` en
    `Sandbox.create()` sigue lanzando `UnimplementedError` sin llamar a AWS:
    usa `CustomDomain`. Estado:
    [Novedades](novedades/index.md#disponible-como-experimental).

!!! warning "El índice de metadatos aún no está aceptado en AWS real"
    Los secretos se aceptaron contra AWS real con la release 0.5.0, y las
    trazas OpenTelemetry no llaman a AWS, así que sus tests con un
    exportador en memoria son la prueba completa. El índice está probado
    con DynamoDB simulado, pero su prueba contra AWS real todavía no se ha
    ejecutado: es, junto con el dominio propio (experimental), la única
    función de esta página sin esa aceptación.

!!! note "Fuera de esta página: el bucket de transferencias"
    Exportar `RAYITO_TRANSFER_BUCKET` (o pasar `transfer=`) hace que los
    ficheros de 8 MiB o más y las URLs firmadas pasen por S3, que cobra sus
    peticiones y el almacenamiento temporal. No es una función opcional de
    esta lista, pero es la única variable de entorno con coste propio:
    [Ficheros y S3](files.md#coste-de-s3).

## Ejemplos

Un ejemplo corto por función. Cada una tiene su guía completa, con las
opciones, el coste detallado y los errores.

<a id="secrets-injection"></a>

### Inyección de secretos

Guía completa, reglas de la caché y modelo de amenazas: [Secretos](secrets.md).

=== "Python"

    ```python
    from rayito import Sandbox, SecretCache

    cache = SecretCache(ttl_seconds=300)  # opcional; no llama a AWS
    with Sandbox.create(secrets={"OPENAI_API_KEY": "openai"}, secret_cache=cache) as sbx:
        sbx.commands.run("python agent.py")  # 1 GetSecretValue por TTL, no por comando
        sbx.commands.run("env", secrets={"GH_TOKEN": "gh"})
    ```

=== "TypeScript"

    ```ts
    // npm install @aws-sdk/client-secrets-manager   (peer opcional)
    import { Sandbox, SecretCache } from "rayito";

    const secretCache = new SecretCache({ ttlSeconds: 300 });
    await using sbx = await Sandbox.create({ secrets: { OPENAI_API_KEY: "openai" }, secretCache });
    await sbx.commands.run("python agent.py");
    await sbx.commands.run("env", { secrets: { GH_TOKEN: "gh" } });
    ```

**El código del sandbox puede leer un secreto inyectado**: con código no
confiable, usa credenciales de vida corta y mínimo privilegio.

<a id="secrets-crud"></a>

<a id="secret-crud-secrets-manager"></a>

### Gestión de secretos (CRUD)

Cada secreto se factura hasta que lo borras: apagar la función es dejar de
instanciar `SecretStore` **y** `destroy()` los secretos creados.

=== "Python"

    ```python
    import os

    from rayito import SecretStore

    store = SecretStore(region="us-east-1")  # no llama a AWS hasta el primer método
    store.create("openai", os.environ["OPENAI_API_KEY"], metadata={"team": "ml"})
    print([info.name for info in store.list(limit=20).items])
    store.destroy("openai")  # deja de facturar
    ```

=== "TypeScript"

    ```ts
    import { SecretStore } from "rayito";

    const store = new SecretStore({ region: "us-east-1" });
    await store.create("openai", process.env.OPENAI_API_KEY ?? "", { metadata: { team: "ml" } });
    console.log((await store.list({ limit: 20 })).items.map((info) => info.name));
    await store.destroy("openai");
    ```

El shim de E2B (`from rayito.e2b import Secret`) usa el mismo `SecretStore`;
diferencias en [Compatibilidad con E2B](e2b-compat.md#secretos-secret-asyncsecret).

<a id="metadata-index"></a>

### Índice de metadatos (DynamoDB)

Despliega antes la tabla (una vez, $0 en reposo) con
`infra/metadata-index.yaml`. Guía completa: [Índice de
metadatos](funciones-opcionales/indice-de-metadatos.md).

=== "Python"

    ```python
    from rayito import DynamoDbIndex, Sandbox

    idx = DynamoDbIndex("rayito-sandboxes")  # no llama a AWS todavía
    with Sandbox.create(metadata={"user": "42"}, index=idx) as sbx:  # 1 PutItem
        sbx.pause()
        for item in Sandbox.list(metadata={"user": "42"}, states=["SUSPENDED"], index=idx):
            print(item.sandbox_id, item.state)  # 1 BatchGetItem por página
    ```

=== "TypeScript"

    ```ts
    import { DynamoDbIndex, Sandbox } from "rayito";

    const index = new DynamoDbIndex({ tableName: "rayito-sandboxes" });
    await using sbx = await Sandbox.create({ metadata: { user: "42" }, index });
    await sbx.pause();
    for await (const item of Sandbox.list({ metadata: { user: "42" }, states: ["SUSPENDED"], index })) {
      console.log(item.sandboxId, item.state);
    }
    ```

=== "CLI"

    ```bash
    rayito sandbox list --metadata user=42 --state suspended --index-table rayito-sandboxes
    ```

<a id="otel-sdk"></a>

### Trazas OpenTelemetry del SDK

Guía completa, nombres de span, atributos y lo que nunca se registra:
[OpenTelemetry](funciones-opcionales/opentelemetry.md).

=== "Python"

    ```python
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    from rayito import Sandbox

    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))

    with Sandbox.create(tracer_provider=provider) as sbx:  # span "rayito.sandbox.create"
        sbx.commands.run("echo hola")  # span "rayito.commands.run"
    print([span.name for span in exporter.get_finished_spans()])
    ```

=== "TypeScript"

    ```ts
    import { BasicTracerProvider, InMemorySpanExporter, SimpleSpanProcessor } from "@opentelemetry/sdk-trace-base";
    import { Sandbox } from "rayito";

    const exporter = new InMemorySpanExporter();
    const provider = new BasicTracerProvider({ spanProcessors: [new SimpleSpanProcessor(exporter)] });

    {
      await using sbx = await Sandbox.create({ tracerProvider: provider }); // span "rayito.sandbox.create"
      await sbx.commands.run("echo hola"); // span "rayito.commands.run"
    }
    console.log(exporter.getFinishedSpans().map((span) => span.name));
    ```

Sin `tracer_provider=`/`tracerProvider` (por defecto): ningún span y ningún
import de `opentelemetry`/`@opentelemetry/api` en tiempo de ejecución.

<a id="local-proxy"></a>

### `rayito sandbox proxy`

```bash
rayito sandbox proxy microvm-<id> --port 8000
# sirve http://127.0.0.1:8000 -> puerto 8000 del guest,
# renovando el JWE antes de que expire; Ctrl+C para cortar.
```

Escucha sólo en loopback (`127.0.0.1`) salvo que pases `--bind <ip>` junto
con `--allow-remote`: quien llegue a ese puerto usa el sandbox con tu acceso.
No hace falta ninguna opción del SDK: es sólo CLI, sin coste AWS propio.
Guía: [Proxy local](funciones-opcionales/proxy-local.md).

## Funciones con coste AWS

La ficha completa de cada función, con las llamadas exactas a la API de AWS
y el fichero del SDK donde está documentada.

| Función | Estado | Opción Python | Opción TypeScript | Por defecto | Qué activa | Recursos / llamadas AWS | Coste aproximado | IAM necesario | Cómo apagarla | Dónde |
|---|---|---|---|---|---|---|---|---|---|---|
| [Inyección de secretos](#secrets-injection) | disponible (0.5.0) | `secrets=` | `secrets` | `None` / `undefined` | Entrega el valor de uno o más secretos como variable de entorno de un comando, PTY, celda Python, contexto de código o plaza tomada del pool (el código del sandbox puede leerlo); la caché se fija con `secret_cache=SecretCache(...)` / `secretCache` | `secretsmanager:GetSecretValue` una vez por secreto y TTL de `SecretCache` (300 s por defecto; un acierto hace 0 llamadas), nunca una vez por llamada; ningún recurso nuevo | SM: $0,05/10 000 llamadas (≈ $0,04/mes por secreto y proceso con el TTL por defecto) + el propio secreto, $0,40/secreto-mes ([precios de Secrets Manager](https://aws.amazon.com/secrets-manager/pricing/), consultado 2026-09-30, us-east-1) | `secretsmanager:GetSecretValue` (y `DescribeSecret`) sobre `…:secret:rayito/*` en las credenciales del **llamante** (política `RayitoSecretsReader` de `infra/secrets-access.yaml`); `kms:Decrypt` sólo con una **clave KMS gestionada por el cliente** | No pasar `secrets=` / `secrets` ni `secret_cache=` / `secretCache` (o pasar `None`/`undefined`) | `clients/python/src/rayito/_secrets.py` / `clients/typescript/src/secrets/inject.ts` |
| [Gestión de secretos (CRUD)](#secrets-crud) | disponible (0.5.0) | `SecretStore(...)` | `new SecretStore({...})` | sin instanciar = sin cliente boto3/SDK | Crear, actualizar, listar y borrar secretos nativos de Rayito bajo un prefijo (y el shim `Secret`/`AsyncSecret` de E2B) | Un secreto de Secrets Manager por `create`; `secretsmanager:CreateSecret/PutSecretValue/UpdateSecret/DescribeSecret/ListSecrets/DeleteSecret` (`AWS_API_NOTES.md` §19) | SM: $0,40/secreto-mes **hasta `destroy`** + $0,05/10 000 llamadas ([precios de Secrets Manager](https://aws.amazon.com/secrets-manager/pricing/), consultado 2026-09-30, us-east-1) | CRUD de Secrets Manager bajo el prefijo configurado (`rayito/` por defecto; `destroy` necesita `DescribeSecret` además de `DeleteSecret`) y `ListSecrets` en `*` (política `RayitoSecretsAdmin` de `infra/secrets-access.yaml`, que ya incluye las dos); añade `kms:GenerateDataKey`/`kms:Decrypt` sobre la clave si se pasa `kms_key_id=`/`kmsKeyId` (clave gestionada por el cliente); sin ese parámetro, la clave gestionada por AWS no cobra ni exige permiso KMS aparte | No instanciar `SecretStore` / `new SecretStore(...)` (llamadas explícitas únicamente) y `destroy()` los secretos creados | `clients/python/src/rayito/_secrets.py` / `clients/typescript/src/secrets/store.ts` |
| [Índice de metadatos (DynamoDB)](#metadata-index) | implementado en 0.5.0, pendiente de aceptación en AWS real | `index=DynamoDbIndex(...)` | `index: new DynamoDbIndex({...})` | `None` / `undefined` | Escribe una fila inmutable por sandbox (`metadata`, imagen, `startedAt`, TTL) en tu tabla DynamoDB al crearlo (`create()`, `PoolConfig`) y la une con `list-microvms` para filtrar `list()`/`paginate()` por metadatos también sobre `SUSPENDED`, sin sondear `Health` ni despertar nada; también en el shim (`Sandbox.list(..., index=)`, `E2B(index=)`) y la CLI (`--index-table`) | `dynamodb:PutItem` una vez por sandbox creado (condicional) y `dynamodb:BatchGetItem` una vez por página de `list-microvms` al listar con `metadata` + `index`; la tabla la despliegas tú (`infra/metadata-index.yaml`, on-demand, TTL en `expires_at`); nunca `DeleteItem` | DynamoDB on-demand ([precios de DynamoDB on-demand](https://aws.amazon.com/dynamodb/pricing/on-demand/), consultado 2026-09-30, us-east-1): $0,625 por millón de WRU (~1 por `create` ≈ $0,000000625) + $0,125 por millón de RRU (`BatchGetItem` se factura por ítem leído: 0,5 RRU por sandbox candidato, lectura eventualmente consistente de ≤ 4 KB) + $0,25/GB-mes tras 25 GB gratis; borrado por TTL gratis. Ejemplo: 10 000 sandboxes/mes ≈ 10 000 WRU ($0,006) y un `list()` diario sobre ellos ≈ 300 000 ítems × 0,5 = 150 000 RRU ($0,019): ≈ $0,03/mes; tabla vacía $0 | `dynamodb:PutItem` (escritor, política `RayitoIndexWriter`) y `dynamodb:BatchGetItem` (lector, `RayitoIndexReader`) sobre el ARN de la tabla, en las credenciales del **llamante** | No pasar `index=` / `index` (o pasar `None`/`undefined`); borrar el stack de `infra/metadata-index.yaml` para dejar de pagar el almacenamiento | `clients/python/src/rayito/_index.py` / `clients/typescript/src/index/dynamodb.ts` |
| [Trazas OpenTelemetry del SDK](#otel-sdk) | disponible (0.5.0) | `tracer_provider=` | `tracerProvider` | `None` / `undefined` | Instrumenta `create/connect/kill/pause/resume` (instancia y clase/estático), `commands.run`, `run_code`/`runCode` y `files.*` con spans `rayito.*` (`SpanKind.CLIENT`) sobre el `TracerProvider` que ya tengas configurado | Ninguno propio: Rayito no crea ni llama ningún servicio AWS por esto — el coste depende de **tu** exportador (CloudWatch, un collector propio, …) | $0 desde Rayito: el exportador OTel lo paga y lo configura quien lo activa, no este SDK ([precios de CloudWatch](https://aws.amazon.com/cloudwatch/pricing/) sólo si exportas ahí, consultado 2026-09-30) | Ninguno propio de Rayito; el que exija tu exportador OTel | No pasar `tracer_provider=` / `tracerProvider` (o pasar `None`/`undefined`) | `clients/python/src/rayito/_otel.py` / `clients/typescript/src/otel.ts` |
| [Montajes S3](funciones-opcionales/montajes-s3.md) | disponible (0.6.0) | `mounts={"/mnt/data": S3Mount(...)}` | `mounts: { "/mnt/data": new S3Mount({...}) }` | `None` / `undefined` | Monta un bucket S3 (o un prefijo) como carpeta del sandbox con `mount-s3`, sólo en `rayito-base-caps` y sólo los buckets del allowlist de la imagen (`RAYITO_ALLOWED_MOUNT_BUCKETS`) | Ningún recurso nuevo: las peticiones S3 normales (`GetObject`/`ListObjectsV2`, y `PutObject`/`DeleteObject` con escritura) con el execution role; la política IAM sale de la pila `s3-mounts` (`rayito stack deploy s3-mounts`) | $0 propio de Rayito; las peticiones y el almacenamiento normales de S3 del bucket ([precios de S3](https://aws.amazon.com/s3/pricing/), consultado 2026-10-01, us-east-1); la pila es $0 (sólo IAM) | `RayitoS3MountAccess` (`infra/s3-mounts.yaml`) en el **execution role**, acotada a un bucket y sus prefijos | No pasar `mounts=` / `mounts`; `rayito stack destroy s3-mounts` quita la política (no borra objetos) | `clients/python/src/rayito/sandbox_sync/main.py` / `clients/typescript/src/sandbox/sandbox.ts` |
| [Tamaños](funciones-opcionales/tamanos.md) | disponible (0.6.0) | `size="4gb"` | `size: "4gb"` | `None` / `undefined` | Lanza la imagen del tamaño pedido (`rayito-base-4gb`) del catálogo cerrado de cinco tamaños, resuelta en el cliente; la imagen se publica antes con `rayito image publish --sizes` | Ninguno nuevo al lanzar; `get_info()` hace como mucho una `GetMicrovmImageVersion` gratuita por versión y proceso; cada tamaño publicado es una imagen más (`create`/`update-microvm-image` al publicarla); la pila opcional `sizes-guard` es una política IAM | El MicroVM más grande cuesta más por hora: de $0,0315/h (512 MiB) a $0,5044/h (8192 MiB) en baseline ([Límites](limits.md#tamano-cpuram), consultado 2026-09-30, us-east-1); cada tamaño publicado añade storage de snapshot, ≈ $0,04/semana por versión | Ninguno adicional para lanzar; `sizes-guard` (`RayitoRunAllowedSizes`) niega `RunMicrovm` fuera de las imágenes que listes | No pasar `size=` / `size`; borrar las versiones de las imágenes de tamaño con `rayito image prune --image-name ...` y la pila `sizes-guard` si la desplegaste | `clients/python/src/rayito/_sizing.py` / `clients/typescript/src/sizing/sizing.ts` |
| [Eventos y webhooks](funciones-opcionales/eventos-y-webhooks.md) | disponible (0.6.0) | `events=LifecycleEvents(...)` | `events: new LifecycleEvents()` | `None` / `undefined` | `rayd` emite `created`/`paused`/`resumed`/`killed` firmados; la pila los guarda (`get_events`) y los entrega a tus webhooks con la firma de E2B | La pila `events-webhooks` (`rayito events deploy`): secreto HMAC, tabla DynamoDB con streams, 3 Lambdas con sus log groups, filtro de suscripción, scheduler, colas SQS de fallos y 4 políticas IAM; por sandbox, un `secretsmanager:GetSecretValue` de la clave del stack por instancia de `LifecycleEvents` | ≈ $0,40/mes en reposo (el secreto); DynamoDB, Lambda y SQS por uso ([precios de Secrets Manager](https://aws.amazon.com/secrets-manager/pricing/), consultado 2026-09-30, us-east-1) | `EventsLauncherPolicy` (salida de la pila) en las credenciales del **llamante** (`EventsReaderPolicy` para `get_events`, `EventsWebhookAdminPolicy` para los webhooks); el execution role escribe en el log group de la imagen | No pasar `events=` / `events`; `rayito events destroy` borra la pila | `clients/python/src/rayito/sandbox_sync/main.py` / `clients/typescript/src/sandbox/sandbox.ts` |
| [Exportación OTLP](funciones-opcionales/exportacion-otlp.md) | disponible (0.6.0) | `telemetry=TelemetryExport(...)` | `telemetry: new TelemetryExport({...})` | `None` / `undefined` | `rayd` exporta 7 métricas de CPU, memoria y disco por OTLP/HTTP cada `interval_s` (60 s por defecto) | `PutMetricData` por lote exportado desde el sandbox; con `OtlpAuth.execution_role()`, la política de la pila `otlp-export` en el execution role | $0 por la opción; CloudWatch factura la ingesta OTLP a $0,50/GB: ≈ $0,00002 por sandbox-hora con `interval_s=60` (`AWS_API_NOTES.md` Q120, [precios de CloudWatch](https://aws.amazon.com/cloudwatch/pricing/), consultado 2026-10-02) | `cloudwatch:PutMetricData` (`RayitoOtlpExport`) en el **execution role**; con `OtlpAuth.bearer(...)`, leer el secreto del token | No pasar `telemetry=` / `telemetry`; borrar la pila `otlp-export` si ya no la usa ningún sandbox | `clients/python/src/rayito/sandbox_sync/main.py` / `clients/typescript/src/sandbox/sandbox.ts` |
| [Pasarela de secretos](funciones-opcionales/pasarela-de-secretos.md) | disponible (0.6.0) | `gateways={"anthropic": SecretGateway(...)}` | `gateways: { anthropic: new SecretGateway({...}) }` | `None` / `undefined` | `rayd` abre en loopback una pasarela por ruta que inyecta la cabecera real hacia un `upstream` fijo: el código del sandbox usa el secreto sin poder leerlo | `secretsmanager:GetSecretValue` una vez por cabecera y TTL de la `SecretCache`; ningún recurso nuevo | $0,05/10 000 llamadas más el propio secreto si no existía ([precios de Secrets Manager](https://aws.amazon.com/secrets-manager/pricing/), consultado 2026-09-30, us-east-1) | `secretsmanager:GetSecretValue` en las credenciales del **llamante** (`RayitoSecretsReader`) | No pasar `gateways=` / `gateways` | `clients/python/src/rayito/_secret_gateway/_domain.py` / `clients/typescript/src/secret-gateway/domain.ts` |
| [Templates](funciones-opcionales/templates.md) | disponible (0.6.0) | `Template.build(...)` | `Template.build(...)` | sin llamar a `build` = sin cliente | Construye una imagen nueva a partir del DSL de templates, compuesta sobre una imagen `rayito-base` publicada (también `rayito template build`) | `GetMicrovmImageVersion`, `CreateMicrovmImage`/`UpdateMicrovmImage`, `GetMicrovmImage`, `ListMicrovmImageVersions`; S3 `GetObject`/`HeadObject`/`PutObject` del zip; logs sólo si el build falla (`AWS_API_NOTES.md` §27) | Storage de snapshot por versión nueva, ≈ $0,04/semana por versión; el build no se factura aparte; el zip de contexto queda en tu bucket (S3 estándar) | `RayitoTemplateBuilder` (`infra/templates.yaml`, `rayito stack deploy templates`) en las credenciales del **llamante** | No llamar a `Template.build()`; borrar las versiones con `rayito image prune` | `clients/python/src/rayito/_templates/__init__.py` / `clients/typescript/src/templates/dsl.ts` |
| [Volúmenes EFS](funciones-opcionales/volumenes-efs.md) | experimental (0.7.0) | `volumes={"/mnt/datos": EfsVolume(...)}`; `VolumeStore(...)`, `EfsVolumes(...)` | `volumes: { "/mnt/datos": new EfsVolume({...}) }`; `new VolumeStore({...})`, `new EfsVolumes({...})` | `None` / `undefined` | `rayd` monta cada access point con `amazon-efs-utils` (TLS + IAM) en el mismo `ConfigureSandbox` que el resto; sólo en la imagen opcional `rayito-base-caps-efs`, con `execution_role_arn` y un único conector propio en `egress` (nunca `INTERNET_EGRESS`) | `create()`: una `DescribeMountTargets` por sistema de ficheros si el volumen no trae `mount_target_ip`; `VolumeStore`: `CreateAccessPoint`/`DescribeAccessPoints`/`DeleteAccessPoint`; la pila `efs-volumes` (`EfsVolumes.deploy`): sistema de ficheros cifrado, mount targets, grupos de seguridad y conector, dentro de tu VPC | $0 vacío; $0,30/GB-mes (Standard), $0,016/GB-mes (IA), $0,03/GB leído y $0,06/GB escrito (Elastic) ([precios de EFS](https://aws.amazon.com/efs/pricing/), consultado 2026-09-11, us-east-1); la imagen con `amazon-efs-utils` ocupa ≈ 198 MB más (Q122) | El llamante: CRUD de access points y `DescribeMountTargets`; el **execution role**: `CallerPolicyArn` de la pila (`ClientMount`, `ClientWrite` salvo sólo lectura) | No pasar `volumes=` / `volumes`; `EfsVolumes.destroy(delete_file_system=True)` borra la pila y los datos | `clients/python/src/rayito/_volumes/` / `clients/typescript/src/volumes/` |
| [Dominio propio](funciones-opcionales/dominio-propio.md) | experimental (0.7.0, sin aceptación de punta a punta en AWS real) | `CustomDomain(public_domain=...)`, `AsyncCustomDomain(...)` | `new CustomDomain({ publicDomain })` | sin instanciar = sin cliente | Una distribución CloudFront en tu cuenta cuya CloudFront Function enruta `https://<puerto>-<alias>.<tu-dominio>` al sandbox, poniendo ella las cabeceras del proxy de AWS; tú registras cada ruta (`register()`/`unregister()`/`refresh()`) | La pila `custom-domain` (`CustomDomain.deploy`, `rayito domain deploy`): distribución, CloudFront Function y KeyValueStore; por ruta, `DescribeKeyValueStore`/`PutKey`/`DeleteKey` (firmadas con SigV4A) | $0 en reposo; ~$0,085/GB + $0,0075/10 000 peticiones HTTPS, ~$0,10 por millón de invocaciones de la Function y ~$0,50 por millón de lecturas del KeyValueStore; ~$5 por millón de `PutKey`/`DeleteKey` ([precios de CloudFront](https://aws.amazon.com/cloudfront/pricing/), cifras de lista sin medir con una distribución real, us-east-1) | El llamante: `cloudformation:*Stack*` y los permisos de CloudFront para crear y borrar la distribución, la Function y el KeyValueStore; `cloudfront-keyvaluestore:DescribeKeyValueStore/PutKey/DeleteKey` sobre el KeyValueStore de la pila | No instanciar `CustomDomain`; `CustomDomain.destroy()` o `rayito domain destroy` borra la pila | `clients/python/src/rayito/_custom_domain/` / `clients/typescript/src/custom-domain/` |
| [Panel de gestión (SDK)](funciones-opcionales/panel-de-gestion.md) | publicado en 0.12.0, aceptado en AWS real el 2026-10-14; el panel local de la CLI de TypeScript (`npx rayito dashboard`), publicado en 0.13.0 y aceptado en AWS real el 2026-10-16 | `Quotas(...)`, `UsageCost(...)`, `Fleet(...)` (y `AsyncQuotas`, `AsyncUsageCost`, `AsyncFleet`) | `new Quotas({...})`, `new UsageCost({...})`, `new Fleet({...})` | sin instanciar o sin llamar = sin cliente; las acciones, `allow_actions=False` / `allowActions: false` | Lee las cuotas aplicadas y la memoria asignada contra la cuota de la región, el coste facturado de MicroVMs (Cost Explorer, por tipo de uso, con caché local) y estimado por sandbox, y el inventario, las imágenes, los eventos y las acciones masivas (matar, suspender, reanudar) con una cola que respeta los TPS | Sólo lecturas, ningún recurso: `ListServiceQuotas`, `ListMicrovms`, `GetMicrovmImageVersion`, `ListMicrovmImages`, `ListMicrovmImageVersions` y, con `billed()`, `ce:GetCostAndUsage` (us-east-1); con `allow_actions=True`, `TerminateMicrovm`/`SuspendMicrovm`/`ResumeMicrovm` (`AWS_API_NOTES.md` §30) | $0 las lecturas; Cost Explorer $0,01 por petición ([precios de Cost Explorer](https://aws.amazon.com/aws-cost-management/aws-cost-explorer/pricing/), consultado 2026-10-13, us-east-1), con caché de 24 h ≈ 30 peticiones/mes ≈ $0,30; la estimación por sandbox es gratuita y nunca sustituye a la factura | `servicequotas:ListServiceQuotas` y las lecturas de Lambda MicroVMs; `ce:GetCostAndUsage` sólo para `billed()`; `lambda:TerminateMicrovm`/`SuspendMicrovm`/`ResumeMicrovm` sólo para las acciones | No llamar a sus métodos; no pasar `allow_actions=True` | `clients/python/src/rayito/_dashboard/` / `clients/typescript/src/dashboard/` |
| [Panel en tu cuenta (pila)](funciones-opcionales/panel-en-tu-cuenta.md) | publicado en 0.12.0, aceptado en AWS real el 2026-10-14, también la variante `AuthMode=aws-iam` | `OptionalStacks().deploy("dashboard", artifact_bucket=...)` | `new OptionalStacks().deploy("dashboard", { artifactBucket })` | sin desplegar = ningún recurso; desplegada, de sólo lectura y sin Cost Explorer | Una Lambda con Function URL sirve la página y la API del panel con inicio de sesión de Cognito (token validado en la función antes de enrutar), rol de lectura y rol de acciones separados (`AllowActions=true`), auditoría de cada acción en DynamoDB, caché del coste facturado y un tema propio (`ThemeJson` / SSM) | `AWS::Lambda::Function` y `Url`, `AWS::Cognito::UserPool` (+ cliente, dominio y grupo), 2 `AWS::DynamoDB::Table`, `AWS::IAM::Role` (1 o 2), `AWS::Logs::LogGroup`, y con `ThemeJson` un `AWS::SSM::Parameter`; sin CloudFront ni buckets | ≈ $0 en reposo; por uso: Lambda por petición (URL pública: el tráfico sin autenticar también factura, acótalo con `ReservedConcurrency`), DynamoDB por acción, con `EnableCostExplorer=true` Cost Explorer $0,01 por consulta con caché de 24 h (peor caso ≈ $6/mes) y, con un tema o logo en parámetros SSM `Advanced`, $0,05/mes cada uno más interacciones de API ([Lambda](https://aws.amazon.com/lambda/pricing/), [Cognito](https://aws.amazon.com/cognito/pricing/), [SSM](https://aws.amazon.com/systems-manager/pricing/), [Cost Explorer](https://aws.amazon.com/aws-cost-management/aws-cost-explorer/pricing/), consultados 2026-10-13, us-east-1) | `cloudformation:*Stack` con `CAPABILITY_IAM` para desplegar; lectura de MicroVMs y cuotas para la función; `ce:GetCostAndUsage` sólo con `EnableCostExplorer=true`; `TerminateMicrovm`/`SuspendMicrovm`/`ResumeMicrovm` sólo en el rol de acciones | No desplegarla, o `rayito stack destroy dashboard` | `clients/python/src/rayito/_stacks/components/dashboard.py` / `clients/typescript/src/stacks/components/dashboard.ts` |
| [Dashboard de CloudWatch](funciones-opcionales/panel-extras.md#dashboard-de-cloudwatch) | publicado en 0.12.0, aceptado en AWS real el 2026-10-14 (la orden `dashboard cloudwatch` de la CLI de TypeScript, publicada en 0.13.0 y aceptada el 2026-10-16); las consultas PromQL de las gráficas OTLP se comprobaron por API, no su dibujo en la consola | `OptionalStacks().deploy("dashboard-cloudwatch")` (o `rayito dashboard cloudwatch`) | `new OptionalStacks().deploy("dashboard-cloudwatch")` | sin desplegar = ningún recurso; `rayito dashboard cloudwatch` sólo imprime | El JSON de un dashboard de CloudWatch con widgets de Logs Insights sobre `/rayito/*` y, con `IncludeOtlp`/`IncludeQuota` (`--otlp`/`--quota`), gráficas de las métricas OTLP y del uso de cuota | Un `AWS::CloudWatch::Dashboard`; imprimirlo no hace ninguna llamada | $0 con menos de 4 dashboards, $3/mes por dashboard desde el cuarto; Logs Insights $0,005 por GB escaneado al abrir o refrescar ([CloudWatch](https://aws.amazon.com/cloudwatch/pricing/), consultado 2026-10-13, us-east-1) | `cloudwatch:PutDashboard`, `GetDashboard`, `DeleteDashboards` para desplegar; `logs:StartQuery` y `logs:GetQueryResults` para verlo | No desplegarla, o `rayito stack destroy dashboard-cloudwatch` | `clients/python/src/rayito/_dashboard/_cloudwatch_board.py` / `clients/typescript/src/stacks/components/dashboard-cloudwatch.ts` |
| [Varias cuentas en el panel](funciones-opcionales/panel-extras.md#varias-cuentas) | publicado en 0.12.0, aceptado en AWS real el 2026-10-14 | `rayito dashboard --account NOMBRE=<ARN>`; `OptionalStacks().deploy("dashboard-read-role", parameters={...})` | `new OptionalStacks().deploy("dashboard-read-role", { parameters })` | sin `--account` no se llama a STS; el rol no existe hasta desplegarlo | Una vista de otras cuentas en el panel local, de sólo lectura y sin coste facturado, con un rol que se asume y un `ExternalId` opcional | En la cuenta destino un `AWS::IAM::Role`; en el panel un `sts:AssumeRole` por cuenta y las lecturas del panel | $0 ([IAM](https://aws.amazon.com/iam/pricing/), consultado 2026-10-13) | `sts:AssumeRole` sobre los roles; cada rol sólo `ListMicrovms`, `ListMicrovmImages`, `ListMicrovmImageVersions`, `GetMicrovmImageVersion` y `ListServiceQuotas` | No pasar `--account`, o `rayito stack destroy dashboard-read-role` en cada cuenta destino | `clients/python/src/rayito/_dashboard/_accounts.py` / `clients/python/src/rayito/_stacks/components/dashboard_read_role.py` |
| [Avisos de cuota](funciones-opcionales/panel-extras.md#avisos-de-cuota) | publicado en 0.12.0, aceptado en AWS real el 2026-10-13 | `OptionalStacks().deploy("quota-alerts", artifact_bucket=...)` | `new OptionalStacks().deploy("quota-alerts", { artifactBucket })` | sin desplegar = ningún recurso | Una comprobación programada que publica en SNS cuando la memoria asignada cruza un umbral de la cuota de la región (un aviso por cruce) y publica el uso como métrica personalizada | `AWS::Lambda::Function`, `AWS::Events::Rule`, `AWS::SNS::Topic`, 1 `AWS::DynamoDB::Table`, `AWS::IAM::Role`, `AWS::Logs::LogGroup` | ≈ $0,30/mes por la métrica; Lambda, EventBridge, DynamoDB y SNS en capas gratuitas ([CloudWatch](https://aws.amazon.com/cloudwatch/pricing/), [SNS](https://aws.amazon.com/sns/pricing/), consultados 2026-10-13, us-east-1) | `CAPABILITY_IAM` para desplegar; la función, lecturas de MicroVMs y cuotas, `sns:Publish` sobre su tema, `GetItem`/`PutItem` sobre su tabla y `cloudwatch:PutMetricData` con `cloudwatch:namespace` fijo | No desplegarla, o `rayito stack destroy quota-alerts` | `clients/python/src/rayito/_stacks/components/quota_alerts.py` / `clients/typescript/src/stacks/components/quota-alerts.ts` |

Una fila pasa a "disponible" cuando la función está aceptada contra AWS
real y publicada en una versión (ver el aviso de [De un vistazo](#de-un-vistazo));
entre la aceptación y la release dice «aceptado en AWS real» y la fecha.

## Sin coste AWS

| Función | Opción | Qué activa | Recursos AWS | Coste | Dónde |
|---|---|---|---|---|---|
| [`rayito sandbox proxy`](#local-proxy) | CLI `rayito sandbox proxy <id> --port N` (Python y TypeScript) | Sirve un puerto del guest en `127.0.0.1` (otra interfaz sólo con `--bind` + `--allow-remote`), renovando el JWE de la cabecera `x-aws-proxy-auth` antes de que expire | `lambda:GetMicrovm` (una vez, para el endpoint) + `lambda:CreateMicrovmAuthToken` (~1 cada 45 min por proxy en marcha); IAM: `lambda:GetMicrovm` y `lambda:CreateMicrovmAuthToken` sobre el MicroVM (ya en la `CallerPolicy` de `infra/iam.yaml`) | $0: ambas llamadas son gratuitas (cuotas de 100 y 50 TPS, `AWS_API_NOTES.md` §11); si el sandbox estaba suspendido, despertarlo por auto-resume factura su cómputo normal más la lectura del snapshot al reanudar, no el proxy en sí | `clients/python/src/rayito/cli/_proxy.py` (CLI) |
| [`rayito-mcp`](mcp.md) | ejecutable `rayito-mcp` (Python: `pip install "rayito[mcp]"`; TypeScript: `npx -y -p rayito -p @modelcontextprotocol/server -p zod rayito-mcp`) | Crea un sandbox por proceso en la primera herramienta que lo necesita y lo termina al cerrar; seis herramientas (`run_code`, `run_command`, `read_file`, `write_file`, `list_files`, `list_sandboxes`) | `RunMicrovm` al primer uso, `TerminateMicrovm` al cerrar, y las llamadas de cada herramienta; sin recursos que persistan | el de un MicroVM en ejecución mientras se usa más un snapshot por suspensión ([modelo de costes](cost.md)); con `RAYITO_MCP_IDLE_SECONDS=0`, sin auto-suspensión | `clients/python/src/rayito/mcp/` / `clients/typescript/src/mcp/` |

No consume cuota ni dinero adicional: reutiliza llamadas gratuitas que el
SDK ya hace. Guía: [Proxy local](funciones-opcionales/proxy-local.md).

<a id="convencion-de-opt-in"></a><a id="la-plantilla-de-cada-opcion-de-coste"></a>

??? info "Para quien añade una función opcional a Rayito"
    La regla de esta página es el ADR-014 de
    [`ARCHITECTURE.md`](https://github.com/alejandro-cedeno-10/rayito/blob/main/ARCHITECTURE.md#adr-014--componentes-opcionales-en-la-cuenta-del-cliente).
    Toda función opcional nueva sigue cuatro reglas, las mismas en Python y
    TypeScript (`clients/python/src/rayito/_optional.py` y
    `clients/typescript/src/optional.ts`):

    - **(a) Un kwarg nombrado por función.** Como mucho, un segundo kwarg con el
      objeto de configuración. `None`/`undefined` es "apagada"; ninguna variable
      de entorno, fichero de configuración ni setter global la enciende; activar
      una opción nunca cambia el significado de otro kwarg.
    - **(b) Objeto de configuración inmutable y reutilizable.** Su nombre de
      clase dice el servicio de AWS (`DynamoDbIndex`, `SecretCache`,
      `SecretStore`) y crea su cliente boto3 / AWS SDK **de forma perezosa, en su
      primer uso** — nunca en el constructor del objeto de configuración ni en el
      del `Sandbox`.
    - **(c) Peers opcionales de TypeScript cargados bajo demanda.** Los clientes
      de AWS SDK v3 que sólo usan estas funciones (`@aws-sdk/client-secrets-manager`,
      `@aws-sdk/client-dynamodb`) son peerDependencies **opcionales**: se cargan
      con `import()` dinámico sólo dentro de la función ya activada
      (`loadOptionalPeer`), nunca a nivel superior de un módulo.
      `@opentelemetry/api` también es un peer opcional, pero sólo para tipos:
      `src/otel.ts` lo usa con `import type` (se borra en el build), el SDK usa
      directamente el `tracerProvider` que le pasa el llamante y no carga nada
      de `@opentelemetry/api` en tiempo de ejecución. En Python, el equivalente
      es `rayito[otel]` vía `require_module`, que importa `opentelemetry.trace`
      sólo al activar `tracer_provider=`.
    - **(d) Bloque de docstring "Coste y activación".** Toda opción de coste lo
      lleva, con la plantilla siguiente.

    **La plantilla.** El docstring (Python) o TSDoc (TypeScript) de la
    opción que activa cada función lleva un bloque titulado literalmente
    **"Coste y activación"**:

    ```text
    Coste y activación
    -------------------
    Activa: <qué hace exactamente esta opción>
    Recursos y llamadas AWS: <qué crea o llama, con nombres de API reales>
    Coste aproximado: <cifra, región y fecha, con enlace a la página de precios>
    IAM: <permisos mínimos necesarios>
    Cómo apagarla: <qué valor la deja apagada — normalmente None/undefined>
    Ejemplo:
        <fragmento runnable de ≤ 10 líneas>
    ```

    `scripts/tests/test_optional_features_docs.py` comprueba, para cada fila
    marcada "disponible" en la tabla de [Funciones con coste AWS](#funciones-con-coste-aws), que el símbolo de opción citado
    aparece en el fichero SDK de su columna "Dónde" dentro de un mismo bloque
    "Coste y activación" que trae, además, los seis apartados de la plantilla.
