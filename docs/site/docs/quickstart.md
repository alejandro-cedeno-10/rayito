# Quickstart

## Instalación

```bash
pip install rayito        # o: uv add rayito
```

Python ≥ 3.11. Dependencias de runtime: `grpcio`, `protobuf` y `boto3`.

## Credenciales

El SDK usa las credenciales de AWS de la sesión de `boto3`: un perfil, variables
de entorno o el rol de la máquina. No hay API key de Rayito.

```bash
export AWS_PROFILE=<perfil> AWS_REGION=us-east-1
export RAYITO_TEMPLATE=rayito-base        # nombre o ARN de la imagen
```

`RAYITO_TEMPLATE` evita pasar `template=` en cada `create()`. Un nombre se
resuelve al ARN `arn:aws:lambda:<region>:<cuenta>:microvm-image:<nombre>` con
la identidad de la sesión (`sts:GetCallerIdentity`).

Lo primero que conviene ejecutar en una cuenta nueva es el diagnóstico de la
[CLI](cli.md): comprueba credenciales, región, cuotas, IAM, bucket, la imagen
y el agente, y dice qué falta antes del primer `create()`.

```bash
pip install "rayito[cli]"
rayito doctor --template rayito-base        # --launch prueba un sandbox efímero (≈ $0,002)
```

## La imagen

Cada sandbox arranca desde una versión de la imagen `rayito-base`, que lleva
`rayd` (el agente) y el sidecar de kernels. La imagen se construye desde
`image/Dockerfile` y se publica con `make image-publish` (ver el `README.md`
del repositorio) o, lo que es lo mismo, con `rayito image publish --artifact
image/rayito-image.zip --base-image-version 1 --bucket <bucket>` ([CLI](cli.md)).
Los metadatos por sandbox necesitan una imagen de M6 o posterior; sobre una
anterior se leen vacíos; `rayito doctor` lo comprueba en `compatibility`.

## Primer sandbox

```python
from rayito import Sandbox

with Sandbox.create(timeout=900) as sbx:
    print(sbx.commands.run("echo hola").stdout)          # "hola\n"
    sbx.files.write("/home/user/a.txt", "contenido")
    print(sbx.files.read("/home/user/a.txt"))
    print(sbx.run_code("x = 40; x + 2").text)             # "42"
```

`with` llama a `kill()` al salir (`terminate-microvm`). Sin `with`, llama a
`sbx.kill()` explícitamente: un sandbox huérfano vive hasta `timeout`
(3600 s por defecto, tope 8 h) y factura mientras tanto.

## Reconectar desde otro proceso

```python
sbx = Sandbox.create()
sandbox_id, token = sbx.sandbox_id, sbx.access_token   # guárdalos

again = Sandbox.connect(sandbox_id, access_token=token)
again.run_code("x")
```

`connect()` no extiende la vida del sandbox y reanuda uno pausado.

## Siguiente

- [Conceptos](concepts.md): vida vs. idle, tokens, canales, streams.
- [Compatibilidad con E2B](e2b-compat.md): si vienes de `e2b_code_interpreter`.
- [Modelo de costes](cost.md): lo que cuesta cada operación, medido.
