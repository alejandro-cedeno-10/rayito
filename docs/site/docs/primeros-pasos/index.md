# Primeros pasos

En unos 15 minutos tendrás un sandbox corriendo en tu cuenta de AWS (casi
todo es esperar a que AWS construya la imagen). Esta
sección sigue un camino lineal: cada página termina donde empieza la
siguiente.

## Qué necesitas

| Requisito | Detalle |
|---|---|
| Una cuenta de AWS | con permisos para desplegar una pila de CloudFormation (IAM) y crear un bucket de S3 |
| Una región con Lambda MicroVMs | `us-east-1`, `us-east-2`, `us-west-2`, `eu-west-1`, `eu-central-1`, `eu-north-1`, `ap-northeast-1`, `ap-south-1`, `ap-southeast-1` o `ap-southeast-2` |
| La AWS CLI v2 | `aws --version`; la usan los pasos de [Configurar AWS](configurar-aws.md) (`aws sts`, `aws s3 mb`, `aws cloudformation deploy`). [Instalarla](https://docs.aws.amazon.com/cli/latest/userguide/getting-started-install.html) |
| `curl` | para descargar la plantilla de IAM y la imagen de la release (viene con macOS y casi todas las distribuciones Linux) |
| [`cosign`](https://docs.sigstore.dev/cosign/system_config/installation/) ≥ 2.x | para comprobar la firma de la imagen de la release antes de publicarla ([Verificar una release](../verify.md)) |
| Credenciales de AWS en tu máquina | un perfil (`aws configure` o `aws configure sso`), variables de entorno o el rol de la máquina |
| Python ≥ 3.11 **o** Node ≥ 20 | el SDK de Python (`rayito` en PyPI) o el de TypeScript (`rayito` en npm) |
| Python ≥ 3.11 para la CLI | aunque uses TypeScript, la CLI `rayito` (publicar la imagen, `rayito doctor`) es Python |

No hace falta Docker en tu máquina: AWS construye la imagen del sandbox a
partir de un zip. No hay API key de Rayito ni cuenta que crear en ningún
otro sitio.

## La ruta recomendada

1. [**Instalación**](instalacion.md): el SDK, la CLI y los extras
   opcionales.
2. [**Configurar AWS**](configurar-aws.md): IAM, un bucket y la imagen
   `rayito-base` en tu cuenta. Se hace una vez por cuenta y región.
3. [**Primer sandbox**](../quickstart.md): comandos, ficheros, código y
   reconexión desde otro proceso.
4. [**Conceptos**](../concepts.md): qué corre dónde, los tres relojes de un
   sandbox, los dos tokens y la reconexión.

Cuando termines, las [Guías](../guias/index.md) explican cada función (entre
ellas, [un agente de código dentro del sandbox](../guias/agente-en-el-sandbox.md))
y la [Referencia](../referencia/index.md) da todos los parámetros.

!!! tip "¿Vienes de E2B?"
    Haz igualmente [Configurar AWS](configurar-aws.md): el SDK necesita una
    imagen en tu cuenta. Después, [Migrar desde E2B](../migrar-desde-e2b/index.md)
    te dice qué import cambiar y qué se comporta distinto.

!!! info "Cuánto cuesta probarlo"
    Un sandbox de 2 GB cuesta ≈ $0,126 por hora mientras corre (us-east-1,
    2026-09) y cada versión de imagen publicada ≈ $0,04 por semana de
    almacenamiento. Recorrer esta sección entera cuesta unos céntimos. Detalle
    y ejemplos en [Precios](../cost.md#cuanto-cuesta-con-ejemplos).
