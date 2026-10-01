# Referencia de Python

Generada desde los docstrings del paquete `rayito` (`pip install rayito`).
Todo lo que aparece aquí se importa desde `rayito` (o desde `rayito.e2b`
para el shim de E2B); los módulos con guion bajo son internos.

| Página | Qué contiene |
|---|---|
| [Sandbox](referencia/python/sandbox.md) | `Sandbox` y `AsyncSandbox`: crear, conectar, ejecutar, pausar, destruir |
| [Subclientes](referencia/python/subclientes.md) | `sbx.commands`, `sbx.files`, `sbx.pty`, `sbx.git`, sus handles y el paginador del listado |
| [Pool](referencia/python/pool.md) | `SandboxPool`, `AsyncSandboxPool`, `PoolConfig` y los backends |
| [Modelos](referencia/python/modelos.md) | los tipos de datos: `SandboxInfo`, `Execution`, `Result`, `EntryInfo`, `S3Staging`… |
| [Opcionales](referencia/python/opcionales.md) | `SecretStore`, `SecretCache`, `SecretRef` y `DynamoDbIndex` (apagados por defecto, con coste) |
| [Excepciones](referencia/python/excepciones.md) | la jerarquía de errores |
| [Shim E2B](referencia/python/shim-e2b.md) | `rayito.e2b`: el SDK de E2B 2.x sobre Rayito |

Convenciones:

- Los tiempos van en **segundos** (`timeout=60`); en TypeScript, en
  milisegundos (`timeoutMs: 60_000`).
- `Sandbox` y `AsyncSandbox` tienen la misma superficie; en `AsyncSandbox`
  cada método es una corrutina.
- Varios métodos funcionan como método de instancia y como método de clase
  con el id del sandbox (`sbx.kill()` y `Sandbox.kill(sandbox_id)`).

Equivalencias con TypeScript: [Referencia de TypeScript](referencia/typescript.md).
