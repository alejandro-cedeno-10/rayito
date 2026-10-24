# Panel: dashboard de CloudWatch, varias cuentas y avisos de cuota

Tres extras opcionales del [panel de gestión](panel-de-gestion.md) (ADR-026,
fase 3). Ninguno se activa solo y ninguno necesita un servidor de Rayito: lo
que crean es tuyo y se borra con `rayito stack destroy`.

Los pasos de cada extra van en su sección. Sólo la vista multicuenta lleva
captura: el dashboard de CloudWatch y los avisos de SNS se ven en la consola de
AWS, que muestra datos de tu cuenta, así que aquí van como texto y salida de
ejemplo. La [guía con capturas del panel](panel-de-gestion.md#recorrido) cubre
el resto, y la entrada **Ayuda** del panel la resume.

| Extra | Qué es | Dónde corre |
|---|---|---|
| [Dashboard de CloudWatch](#dashboard-de-cloudwatch) | El JSON de un dashboard con logs de los sandboxes, métricas OTLP y uso de cuota | Se imprime (sin AWS) o se despliega la pila `dashboard-cloudwatch` |
| [Varias cuentas](#varias-cuentas) | `rayito dashboard --account` ve otras cuentas con un rol de sólo lectura | Tu portátil; la pila `dashboard-read-role` va en cada cuenta destino |
| [Avisos de cuota](#avisos-de-cuota) | Una Lambda programada publica en SNS al cruzar un umbral de la cuota | La pila `quota-alerts`, en tu cuenta |

## Dashboard de CloudWatch { #dashboard-de-cloudwatch }

!!! info "Coste y activación"
    - **Por defecto**: apagado. `rayito dashboard cloudwatch` sólo imprime
      texto: no necesita credenciales ni hace ninguna llamada a AWS. La pila
      `dashboard-cloudwatch` no se crea hasta que la despliegas.
    - **Activa**: `rayito stack deploy dashboard-cloudwatch`, o
      `aws cloudwatch put-dashboard` con el JSON que imprime la CLI.
      `IncludeOtlp=true` y `IncludeQuota=true` añaden las gráficas opcionales.
    - **Recursos y llamadas AWS**: un único `AWS::CloudWatch::Dashboard`; ni
      función, ni rol, ni datos.
    - **Coste aproximado**: **generar el JSON es gratis** (`rayito dashboard
      cloudwatch` no llama a AWS); **desplegarlo cuesta**. $0 mientras tengas
      menos de 4 dashboards (los 3 primeros, de hasta 50 métricas, son
      gratuitos); por encima de esos 3, $3 al mes por dashboard. Cada widget
      de Logs Insights ejecuta su consulta cada vez que abres o refrescas el
      dashboard y CloudWatch factura los datos escaneados ($0,005 por GB):
      acota el intervalo de tiempo. `--quota` (o `IncludeQuota=true`) grafica
      la métrica de la pila `quota-alerts`, que cuesta ~$0,30 al mes aparte;
      las consultas PromQL de `--otlp` desde la consola no se facturan
      ([precios de CloudWatch](https://aws.amazon.com/cloudwatch/pricing/),
      consultados el 2026-10-13, us-east-1).
    - **IAM**: quien despliega, lo de [Pilas opcionales](pilas-opcionales.md)
      más `cloudwatch:PutDashboard`, `GetDashboard` y `DeleteDashboards`
      (CloudFormation actúa con sus permisos); quien lo mira, `logs:StartQuery`
      y `logs:GetQueryResults` sobre los grupos.
    - **Cómo apagarla**: `rayito stack destroy dashboard-cloudwatch`, o
      `aws cloudwatch delete-dashboards`.
    - **Ejemplo**: el de la sección siguiente.

El dashboard lleva cinco widgets de Logs Insights (más un encabezado) sobre los grupos `/rayito/*`
(el que usa el SDK con `logging="cloudwatch"`): líneas por intervalo, errores
por intervalo, líneas por grupo, streams más ruidosos y errores recientes. Con
`--otlp` añade CPU, memoria y disco de las
[métricas OTLP](exportacion-otlp.md) de `rayd`; con `--quota`, la gráfica del
uso de la cuota de memoria con los umbrales de aviso y crítico como líneas.

=== "CLI"

    ```bash
    # Sólo imprime: ninguna llamada a AWS, ni siquiera credenciales
    rayito --region us-east-1 dashboard cloudwatch > rayito-board.json
    rayito dashboard cloudwatch --otlp --quota --output rayito-board.json
    # Otros grupos de logs, o un prefijo propio
    rayito dashboard cloudwatch --log-group /rayito/base --log-group /rayito/agent
    rayito dashboard cloudwatch --log-group-prefix /rayito/team-a/
    # Crearlo es cosa tuya:
    aws cloudwatch put-dashboard --dashboard-name "$NOMBRE_DEL_DASHBOARD" \
      --dashboard-body file://rayito-board.json
    ```

=== "Python"

    ```python
    from rayito import OptionalStacks

    stacks = OptionalStacks()  # no llama a AWS
    status = stacks.deploy(
        "dashboard-cloudwatch",
        parameters={"IncludeOtlp": "true", "IncludeQuota": "true"},
        tags={"Owner": "mi-equipo"},  # las etiquetas que tu organización exija
    )
    print(status.outputs["DashboardName"])
    ```

=== "TypeScript"

    ```ts
    import { OptionalStacks } from "rayito";

    const status = await new OptionalStacks().deploy("dashboard-cloudwatch", {
      parameters: { IncludeOtlp: "true", IncludeQuota: "true" },
      tags: { Owner: "mi-equipo" },
    });
    console.log(status.outputs.DashboardName);
    ```

**El nombre del dashboard.** La pila no fija un nombre: con el parámetro
`DashboardName` vacío (el valor por defecto) el dashboard se llama como la pila
(`rayito-dashboard-cloudwatch` salvo que uses `--stack-name`), que ya es único
en la región, así que desplegar no choca con un dashboard tuyo llamado
`rayito`. Para elegir otro, pásalo como parámetro (`--param
DashboardName=<nombre>`). Una pila desplegada antes de este cambio conserva el
nombre que ya tenía: redesplegar no la renombra.

El JSON que imprime la CLI y el que despliega la pila salen del mismo
generador, así que no pueden divergir (un test lo comprueba). El nombre de
grupo y el prefijo se validan antes de construir ninguna consulta: sólo letras,
números y `_ . # - /`.

### Probar las gráficas OTLP { #probar-otlp }

Las gráficas de `--otlp` (o `IncludeOtlp=true`) sólo tienen datos si algún
sandbox exporta sus [métricas OTLP](exportacion-otlp.md), y esa exportación
está **apagada por defecto**. Para ver las gráficas con datos:

1. Despliega la política de exportación una vez en la cuenta:
   `rayito stack deploy otlp-export`. No cuesta nada en reposo.
2. Crea un sandbox con `telemetry=TelemetryExport(...)` (TypeScript:
   `telemetry: new TelemetryExport({...})`) sobre la imagen `rayito-base-caps`
   con tu rol de ejecución, tal como muestra el
   [ejemplo de la exportación OTLP](exportacion-otlp.md#ejemplo-rapido). Cada
   lote cuesta unos $0,00002 por sandbox-hora a `interval_s=60`.
3. Espera al menos dos intervalos (`interval_s`, 60 s por defecto) y comprueba
   en el propio sandbox que el envío funciona:
   `sbx.get_telemetry_status()` debe dar `exported` mayor que cero y
   `last_error_class` vacío.
4. Despliega o actualiza el dashboard con `IncludeOtlp=true` y ábrelo en la
   consola de CloudWatch. Las tres gráficas (CPU, memoria y disco) usan
   consultas PromQL con el nombre de la métrica entre comillas, por los
   puntos: `avg({"rayito.sandbox.cpu.used_pct"})`. Si exportas con
   `names="e2b"`, las métricas se llaman `e2b.sandbox.*` y estas gráficas
   quedan vacías.
5. Para apagarlo, termina el sandbox (`sbx.kill()`) y, si ya nadie lo usa,
   `rayito stack destroy otlp-export`. Si adjuntaste la política
   `RayitoOtlpExport` a un rol, suéltala antes: CloudFormation no borra una
   política que sigue adjunta (`DELETE_FAILED`).

Las métricas OTLP no salen en `ListMetrics`, así que busca las series en
**Metrics → Query** de la consola con PromQL, no en el explorador clásico. Así
se ven las tres gráficas del dashboard con datos de ejemplo (en tu cuenta
dibujarán lo que exporten tus sandboxes):

![Tres gráficas de ejemplo: CPU en uso, memoria y disco de los sandboxes, con las series en uso y total](../assets/panel/otlp-widgets.png){ loading=lazy }

Para comprobar los datos sin abrir la consola, CloudWatch ofrece una API HTTP
de PromQL (`https://monitoring.<región>.amazonaws.com/api/v1/query` y
`query_range`, firmada con SigV4 de servicio `monitoring`) que acepta la misma
consulta que el widget:

```python
import json
import urllib.parse
import urllib.request

import boto3
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest

session = boto3.Session(region_name="us-east-1")
query = urllib.parse.urlencode({"query": 'avg({"rayito.sandbox.cpu.used_pct"})'})
url = f"https://monitoring.us-east-1.amazonaws.com/api/v1/query?{query}"
request = AWSRequest(method="GET", url=url)
SigV4Auth(session.get_credentials().get_frozen_credentials(), "monitoring", "us-east-1").add_auth(request)
reply = urllib.request.urlopen(urllib.request.Request(url, headers=dict(request.headers)))
print(json.load(reply)["data"]["result"])
```

!!! info "Medido en AWS real"
    Probado el 2026-10-14 en us-east-1 con sandboxes de Python y de TypeScript
    (`interval_s=15`): el primer lote llega en segundos y las siete métricas
    responden a la consulta con el nombre entre comillas; sin comillas, PromQL
    da un error de sintaxis. Cada sandbox es una serie distinta
    (`@resource.sandbox_id`). El `SOURCE logGroups(namePrefix: [...])` de los
    widgets de log se ejecuta en Logs Insights y devuelve resultados. Lo que no
    se pudo comprobar por API es cómo dibuja la consola los widgets PromQL
    (`GetMetricWidgetImage` no los admite). Detalle: `AWS_API_NOTES.md` §30,
    Q167.

## Varias cuentas { #varias-cuentas }

!!! info "Coste y activación"
    - **Por defecto**: apagado. `rayito dashboard` sin `--account` ve sólo la
      cuenta de tus credenciales, y no se llama a STS.
    - **Activa**: `rayito dashboard --account NOMBRE=ARN_DEL_ROL`, repetible
      hasta {{dashboardAccountsMax}} cuentas, después de desplegar
      `dashboard-read-role` en cada cuenta destino.
    - **Recursos y llamadas AWS**: en cada cuenta destino, un `AWS::IAM::Role`
      de sólo lectura. En el panel, un `sts:AssumeRole` por cuenta (al pedir su
      vista por primera vez y al renovar cada hora) y las mismas lecturas que
      el panel de una cuenta: `ListMicrovms`, `ListMicrovmImages`,
      `Get/ListMicrovmImageVersion` y `ListServiceQuotas`.
    - **Coste aproximado**: $0. IAM y STS no facturan; las lecturas no tienen
      precio ([IAM](https://aws.amazon.com/iam/pricing/), consultado el
      2026-10-13).
    - **IAM**: la identidad del panel necesita `sts:AssumeRole` sobre esos
      roles; cada rol destino sólo permite las cinco lecturas anteriores.
    - **Cómo apagarla**: no pases `--account`; `rayito stack destroy
      dashboard-read-role` en cada cuenta destino borra el rol.
    - **Ejemplo**: el de esta sección.

La vista multicuenta es del panel **local** (`rayito dashboard`). Las cuentas
adicionales son **siempre de sólo lectura**: sin acciones, sin coste facturado,
sin logs y sin eventos. El selector de la página sólo muestra los nombres que
elegiste; el ARN del rol y el id de la cuenta no salen por la API.

**1. En cada cuenta destino**, despliega el rol de lectura. Confía en la
identidad que ejecuta el panel (nunca en la raíz de la cuenta) y, si quieres,
exige un `ExternalId`:

=== "CLI"

    ```bash
    rayito --profile <perfil-de-la-cuenta-destino> stack deploy dashboard-read-role \
      --param TrustedPrincipalArn=arn:aws:iam::123456789012:role/rayito-panel \
      --param ExternalId=un-valor-largo-y-aleatorio
    rayito --profile <perfil-de-la-cuenta-destino> stack status dashboard-read-role   # RoleArn
    ```

=== "Python"

    ```python
    from rayito import OptionalStacks

    status = OptionalStacks().deploy(
        "dashboard-read-role",
        parameters={
            "TrustedPrincipalArn": "arn:aws:iam::123456789012:role/rayito-panel",
            "ExternalId": "un-valor-largo-y-aleatorio",
        },
    )
    print(status.outputs["RoleArn"])
    ```

=== "TypeScript"

    ```ts
    import { OptionalStacks } from "rayito";

    const status = await new OptionalStacks().deploy("dashboard-read-role", {
      parameters: {
        TrustedPrincipalArn: "arn:aws:iam::123456789012:role/rayito-panel",
        ExternalId: "un-valor-largo-y-aleatorio",
      },
    });
    console.log(status.outputs.RoleArn);
    ```

**2. En la cuenta del panel**, dale a tu identidad permiso para asumir esos
roles y arranca el panel con ellos:

```json
{
  "Effect": "Allow",
  "Action": "sts:AssumeRole",
  "Resource": "arn:aws:iam::123456789012:role/rayito-dashboard-read-*"
}
```

```bash
rayito dashboard \
  --account staging=arn:aws:iam::123456789012:role/rayito-dashboard-read-AbC123 \
  --external-id un-valor-largo-y-aleatorio
# Panel de Rayito (us-east-1, sólo lectura, más 1 cuenta de sólo lectura). Ábrelo en tu navegador:
```

**3. Cambia de cuenta.** Con una o más cuentas adicionales, la esquina superior
derecha muestra un selector con **Esta cuenta** y el nombre de cada una. Las
adicionales son siempre de **sólo lectura**. La captura usa cuentas de ejemplo.

![Selector de cuenta del panel con una cuenta adicional de ejemplo](../assets/panel/panel-accounts.png){ loading=lazy }

**Sin escribir cuentas en la orden.** Nada de esto exige ARNs en un fichero ni
en el historial del shell: la cuenta destino puede venir sólo de variables de
entorno y armarse al ejecutar. `--account` se lee también de
`RAYITO_DASHBOARD_ACCOUNTS` (entradas `NOMBRE=ARN` separadas por espacios) y el
`ExternalId`, de `RAYITO_DASHBOARD_EXTERNAL_ID`:

```bash
export TARGET_ACCOUNT_ID=<id-de-la-cuenta-destino>   # lo pones tú, nunca en el repo
export TARGET_ROLE_NAME=<nombre-del-rol-de-lectura>  # la salida RoleArn de la pila
export RAYITO_DASHBOARD_ACCOUNTS="staging=arn:aws:iam::${TARGET_ACCOUNT_ID}:role/${TARGET_ROLE_NAME}"
export RAYITO_DASHBOARD_EXTERNAL_ID=<el-mismo-valor-de-la-pila>
rayito dashboard
```

La plantilla, el panel y la política de la identidad son independientes de la
cuenta: sólo el ARN del rol cambia, y llega en tiempo de ejecución. Para
comprobar una cuenta destino de punta a punta: despliega `dashboard-read-role`
en ella (paso 1), exporta las variables, arranca `rayito dashboard` y elige la
cuenta en el selector; una cuenta que rechaza el rol aparece con el código
`AccessDenied`. Si lo que quieres es comprobar que el rol no puede actuar, no
lo pruebes con un id de MicroVM inventado: AWS responde `ResourceNotFoundException`
antes de autorizar. Usa el simulador de IAM (`aws iam simulate-principal-policy`
sobre el rol) o un MicroVM real.

Así se ve una cuenta cuyo rol rechaza el panel (por ejemplo, un `ExternalId`
que no coincide): la página muestra el código de AWS y nada más.

![Selector de cuenta con una cuenta de ejemplo que AWS rechaza: aviso «No se pudo cargar» con el código AccessDenied](../assets/panel/panel-accounts-denied.png){ loading=lazy }

El `ExternalId` es el mismo para todas las cuentas. Si una cuenta rechaza el rol, la página lo dice con el
código de AWS (`AccessDenied`) y sin el mensaje, que nombraría rol y cuenta. La
sesión asumida dura {{dashboardAssumeRoleSessionSeconds}} s y se renueva sola.

La pila `dashboard` **de tu cuenta** no incluye vista multicuenta todavía: sólo
el panel local la tiene.

## Avisos de cuota { #avisos-de-cuota }

!!! info "Coste y activación"
    - **Por defecto**: apagado. Nada se crea hasta que ejecutas
      `rayito stack deploy quota-alerts`.
    - **Activa**: `rayito stack deploy quota-alerts --artifact-bucket <bucket>`.
      `--param NotificationEmail=...` suscribe un correo al tema; `--param
      TopicArn=...` usa un tema tuyo; `WarningPercent` y `CriticalPercent`
      cambian los umbrales y `ScheduleExpression` la frecuencia.
    - **Recursos y llamadas AWS**: una Lambda con su log group, una regla de
      EventBridge, un tema SNS (salvo con `TopicArn`) y su suscripción, una
      tabla DynamoDB bajo demanda con una fila y un rol de IAM. Cada ejecución
      lista los MicroVMs y las cuotas de la región y publica una métrica
      personalizada.
    - **Coste aproximado**: ~$0,30 al mes por la métrica personalizada
      (`{{quotaMetricNamespace}}`); Lambda (~720 ejecuciones al mes con
      `{{quotaAlertsScheduleExpression}}`), EventBridge, DynamoDB y SNS por
      correo caben en sus capas gratuitas
      ([CloudWatch](https://aws.amazon.com/cloudwatch/pricing/),
      [Lambda](https://aws.amazon.com/lambda/pricing/),
      [SNS](https://aws.amazon.com/sns/pricing/); consultados el 2026-10-13,
      us-east-1).
    - **IAM**: quien despliega necesita `CAPABILITY_IAM`. La función sólo lee
      MicroVMs y cuotas; escribe en SNS (`sns:Publish` sobre su tema), en su
      fila de DynamoDB y en su espacio de nombres de CloudWatch
      (`cloudwatch:namespace`).
    - **Cómo apagarla**: `rayito stack destroy quota-alerts`. Borra todo,
      incluido el tema que creó (no el de `TopicArn`).
    - **Ejemplo**: el de esta sección.

=== "CLI"

    ```bash
    rayito stack deploy quota-alerts --artifact-bucket mi-bucket \
      --param NotificationEmail=ops@example.com --param WarningPercent=70
    # Probar el cableado sin esperar a la programación:
    aws lambda invoke --function-name <FunctionName> /dev/stdout
    rayito stack destroy quota-alerts
    ```

=== "Python"

    ```python
    from rayito import OptionalStacks

    stacks = OptionalStacks()  # no llama a AWS
    status = stacks.deploy(
        "quota-alerts",
        artifact_bucket="mi-bucket",
        parameters={"NotificationEmail": "ops@example.com", "WarningPercent": "70"},
        tags={"Owner": "mi-equipo"},
    )
    print(status.outputs["TopicArn"])
    ```

=== "TypeScript"

    ```ts
    import { OptionalStacks } from "rayito";

    const status = await new OptionalStacks().deploy("quota-alerts", {
      artifactBucket: "mi-bucket",
      parameters: { NotificationEmail: "ops@example.com", WarningPercent: "70" },
      tags: { Owner: "mi-equipo" },
    });
    console.log(status.outputs.TopicArn);
    ```

La función mide la memoria asignada (`RUNNING` más suspendidos) contra la cuota
de la región, igual que la pestaña de cuotas del panel, y clasifica el uso con
los umbrales ({{quotaWarningFraction}} y {{quotaCriticalFraction}} de la cuota
por defecto, en porcentaje en la pila). Avisa **una vez por cruce**: cuando el
nivel sube (de normal a alto, de alto a crítico) y no en cada ejecución
mientras se mantiene. Recuerda el nivel más alto alcanzado hasta que el uso
vuelve a normal, así que un uso que oscila alrededor del umbral crítico avisa
una vez y no cada vez que lo cruza; al bajar a normal, el siguiente cruce vuelve
a avisar. Si SNS falla, la ejecución falla y el siguiente ciclo reintenta el aviso.

El mensaje lleva la región, la memoria asignada y la cuota, y cuántos sandboxes
cuentan; nunca ids de sandbox, nombres de imagen ni de cuenta. Si no se pudo
leer la memoria de algún sandbox, lo dice: el uso real puede ser mayor.

Cada pila vigila **una región**: despliega una por región. La métrica
`{{quotaMetricNamespace}}/{{quotaMetricName}}` (dimensión `Region`) es la que
dibuja el [dashboard de CloudWatch](#dashboard-de-cloudwatch) con `--quota`. El
tema SNS no se cifra: el mensaje no lleva datos sensibles.

## IAM mínimo { #iam-minimo }

| Quién | Qué permite | Dónde |
|---|---|---|
| Rol de la función `quota-alerts` | `lambda:ListMicrovms`, `lambda:GetMicrovmImageVersion` (imágenes de la cuenta), `servicequotas:ListServiceQuotas`, `sns:Publish` sobre su tema, `dynamodb:GetItem`/`PutItem` sobre su tabla, `cloudwatch:PutMetricData` sólo con `cloudwatch:namespace` igual a `{{quotaMetricNamespace}}` | `infra/quota-alerts.yaml` |
| Rol `dashboard-read-role` | `lambda:ListMicrovms`, `ListMicrovmImages`, `ListMicrovmImageVersions`, `GetMicrovmImageVersion`, `servicequotas:ListServiceQuotas`; ninguna escritura | `infra/dashboard-read-role.yaml` |
| Identidad del panel local con `--account` | `sts:AssumeRole` sobre esos roles | tu política |

Ninguna de las tres pilas crea CloudFront ni buckets, y la de avisos funciona
aunque la organización no permita crear Lambdas directamente (la Lambda la crea
CloudFormation).

## Ver también

- [Panel de gestión](panel-de-gestion.md) y [Panel en tu cuenta](panel-en-tu-cuenta.md)
- [Pilas opcionales](pilas-opcionales.md)
- [Exportación OTLP](exportacion-otlp.md)
- [API del panel](../referencia/api-del-panel.md)
