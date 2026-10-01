# LangChain y Vercel AI

Rayito como herramienta de un agente: el modelo escribe Python, la
herramienta lo ejecuta en un sandbox con estado y le devuelve el resultado.
Dos adaptadores de unas cincuenta líneas, listos para copiar.

## Cuándo usarlo

- Tu agente usa LangChain (Python) o el Vercel AI SDK (TypeScript) y
  quieres darle un intérprete de código seguro.
- Para hosts que hablan MCP (Claude Code, Claude Desktop, Cursor, VS Code)
  no hace falta código: usa el [servidor MCP](../mcp.md).

## Ejemplo

Cada adaptador crea el sandbox en la primera llamada, lo reutiliza (las
variables sobreviven entre llamadas, como en un notebook) y lo destruye al
salir del proceso. Devuelve al modelo un JSON con `text`, `stdout`,
`stderr` y `error`.

=== "Python"

    Instalación: `pip install rayito langchain` y el paquete del proveedor de
    tu modelo.

    ```python
    --8<-- "docs/examples/langchain_tool.py"
    ```

=== "TypeScript"

    Instalación: `pnpm add rayito ai zod` y el paquete del proveedor de tu
    modelo.

    <!-- noqa: example: importa `ai` y `zod`, que el repositorio no instala; el fichero vive en docs/examples/ -->
    ```ts
    --8<-- "docs/examples/vercel_ai_tool.ts"
    ```

Los dos leen la imagen de `RAYITO_TEMPLATE` y las credenciales de la cadena
estándar de AWS (`AWS_PROFILE`, `AWS_REGION`).

## Paso a paso

1. **Un sandbox por proceso.** La primera llamada a la herramienta crea el
   sandbox con un `timeout` de 900 s; las siguientes reutilizan el mismo
   kernel.
2. **Errores como datos.** Si el código del modelo lanza una excepción, la
   herramienta devuelve `error` con su nombre y valor en vez de fallar: el
   modelo puede leerlo y corregirse.
3. **Limpieza.** El sandbox se mata al salir del proceso (`atexit` en
   Python, `beforeExit` en Node). Si el proceso muere de golpe, el sandbox
   vive hasta su `timeout`.

## Ajustes habituales

- **Un sandbox por conversación**: guarda el sandbox en el estado de la
  sesión en vez de en una variable global, y mátalo al cerrar la sesión.
- **Latencia**: con un [pool](../pool.md), la primera llamada tarda
  < 1 s en vez de 5–6 s.
- **Sin internet**: crea el sandbox en `rayito-base-caps` con
  `allow_internet_access=False` ([Red saliente](../network.md)).
- **Gráficos**: devuelve también `execution.results[i].png` (base64) si tu
  modelo acepta imágenes.

## Ver también

- [Servidor MCP](../mcp.md)
- [Ejecutar código](ejecutar-codigo.md)
- [Seguridad](../security.md): qué puede hacer el código del modelo dentro
  del sandbox
