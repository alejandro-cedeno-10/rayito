# FAQ

## ¿Necesito una API key de Rayito?

No. Rayito no tiene servidor ni cuentas: el SDK usa tus credenciales de AWS
y cada sandbox tiene su propio *access token*, que genera el SDK al crearlo.

## ¿Cuánto cuesta?

Lo que consuma en tu factura de AWS: ≈ $0,126 por hora y sandbox de 2 GB
mientras corre, unos céntimos por lanzamiento y por ciclo de pausa, y
≈ $0,04 por semana por versión de imagen publicada. Ejemplos en
[Costes](../cost.md).

## ¿Cómo elijo CPU y memoria?

Con la imagen. El tamaño es una propiedad de la imagen de Lambda MicroVMs,
no de `create()`: publica una imagen por tamaño
(`rayito image publish --memory-mib 4096 --image-name myimg-4gb`) y elige la
imagen al crear el sandbox ([Límites: tamaño](../limits.md#tamano-cpuram)).

## ¿Puede un sandbox vivir más de 8 horas?

No: 8 h es el máximo de la plataforma y cuenta también el tiempo pausado.
Para seguir con los mismos ficheros, `sbx.reincarnate()` guarda el `HOME` en
S3 y lo restaura en un sandbox nuevo con 8 h frescas; la memoria (variables
del kernel, procesos) no sobrevive ([Persistencia](../persistence.md)).

## ¿Qué regiones?

Las diez con Lambda MicroVMs: `us-east-1`, `us-east-2`, `us-west-2`,
`eu-west-1`, `eu-central-1`, `eu-north-1`, `ap-northeast-1`, `ap-south-1`,
`ap-southeast-1` y `ap-southeast-2`.

## ¿Funciona desde macOS o Windows?

Sí: el SDK y la CLI corren en cualquier sistema con Python ≥ 3.11 o
Node ≥ 20. Lo único que necesita Linux es **compilar** `rayd` desde el
código fuente; publicar la imagen desde el `rayito-image.zip` de la release
no lo necesita.

## ¿Qué lenguajes puedo ejecutar dentro?

Cualquier cosa con `commands.run`. Con `run_code`: Python siempre; bash,
JavaScript y TypeScript (con Deno) en `rayito-base-poly`. R y Java no: R
ocupa 1,4 GB instalado y el kernel de Java no tiene mantenimiento
([Lenguajes y kernels](../kernels.md#r-y-java)).

## ¿Desde qué lenguajes puedo usar Rayito?

Python y TypeScript tienen SDK oficial. Cualquier otro lenguaje con cliente
gRPC y SDK de AWS puede usarlo generando el cliente desde el `.proto`
([Otros lenguajes](../referencia/otros-lenguajes.md)).

## ¿El código del sandbox puede salir a internet?

Por defecto sí. Para cortarlo o limitarlo a ciertos dominios, usa la imagen
`rayito-base-caps` con `allow_internet_access=False` o `network=`, o un
conector de red VPC propio ([Red saliente](../network.md)).

## ¿Puede el código del sandbox leer mis credenciales de AWS?

No, salvo que le des un *execution role* (`execution_role_arn=`), que por
defecto no existe. Las transferencias por S3 las firma tu proceso, no el
sandbox ([Seguridad](../security.md)).

## ¿Qué pasa si mi proceso muere con sandboxes vivos?

Siguen vivos (y facturando) hasta su `timeout`. Con el
[plazo del servidor](../lifecycle.md) (`max_lifetime` u `on_timeout`), el
propio agente los mata o los pausa al vencer, aunque tu proceso no exista.
Para limpiar a mano: `rayito sandbox kill --all`.

## ¿Rayito frente a E2B, Modal o Daytona?

| | Rayito | E2B | Modal | Daytona |
|---|---|---|---|---|
| Dónde corre | tu cuenta de AWS | la nube de E2B (o autoalojado) | la nube de Modal | la nube de Daytona (o autoalojado) |
| Agente en la VM | `rayd` (Rust) | `envd` (Go) | propietario | daemon en Go |
| SDK oficiales | Python, TypeScript | Python, JS/TS | Python (JS y Go vía libmodal) | Python, TypeScript, Ruby, Go, Java |
| API | la de E2B 2.x (shim) + nativa | E2B | Modal | Daytona |
| Servidor que operar | ninguno | ninguno (SaaS) | ninguno (SaaS) | ninguno (SaaS) |

Elige Rayito si quieres que el código y los datos no salgan de tu cuenta de
AWS y no quieres operar un clúster. Fuentes de la tabla:
[Otros lenguajes](../referencia/otros-lenguajes.md#comparado-con-otros-sandboxes).

## ¿Es estable?

Cada versión se acepta contra AWS real, no contra simuladores, y las
releases están firmadas ([Verificar una release](../verify.md)). La única
función opcional sin aceptación en AWS real todavía es el
[índice de metadatos](../funciones-opcionales/indice-de-metadatos.md).
Cambios por versión: [Changelog](../referencia/changelog.md).
