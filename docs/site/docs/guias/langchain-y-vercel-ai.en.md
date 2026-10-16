# LangChain and Vercel AI

Rayito as an agent tool: the model writes Python, the tool runs it in a
stateful sandbox and returns the result. Two adapters of about fifty lines
each, ready to copy.

## When to use it { #cuando-usarlo }

- Your agent uses LangChain (Python) or the Vercel AI SDK (TypeScript) and
  you want to give it a safe code interpreter.
- For hosts that speak MCP (Claude Code, Claude Desktop, Cursor, VS Code)
  you need no code: use the [MCP server](../mcp.md).
- If you want the whole agent (the model loop and its tools) to run
  **inside** the sandbox, with the model behind the secrets gateway, you do
  not need these adapters: use
  [`sbx.agent`](agente-en-el-sandbox.md), with OpenCode or with a
  deepagents (LangChain) graph.

## Example { #ejemplo }

Each adapter creates the sandbox on the first call, reuses it (variables
survive between calls, as in a notebook) and destroys it when the process
exits. It returns to the model a JSON with `text`, `stdout`, `stderr` and
`error`.

=== "Python"

    Install: `pip install rayito langchain` and the provider package for
    your model.

    ```python
    --8<-- "docs/examples/langchain_tool.py"
    ```

=== "TypeScript"

    Install: `pnpm add rayito ai zod` and the provider package for your
    model.

    <!-- noqa: example: importa `ai` y `zod`, que el repositorio no instala; el fichero vive en docs/examples/ -->
    ```ts
    --8<-- "docs/examples/vercel_ai_tool.ts"
    ```

Both read the image from `RAYITO_TEMPLATE` and the credentials from the
standard AWS chain (`AWS_PROFILE`, `AWS_REGION`).

## Step by step { #paso-a-paso }

1. **One sandbox per process.** The first call to the tool creates the
   sandbox with a `timeout` of 900 s; later calls reuse the same kernel.
2. **Errors as data.** If the model's code raises an exception, the tool
   returns `error` with its name and value instead of failing: the model
   can read it and correct itself.
3. **Cleanup.** The sandbox is killed when the process exits (`atexit` in
   Python, `beforeExit` in Node). If the process dies abruptly, the sandbox
   lives until its `timeout`.

## Common adjustments { #ajustes-habituales }

- **One sandbox per conversation**: keep the sandbox in the session state
  instead of a global variable, and kill it when the session closes.
- **Latency**: with a [pool](../pool.md), the first call takes
  < 1 s instead of 5-6 s; each idle slot has a fixed monthly cost
  ([Pricing](../cost.md#el-pool-almacenamiento-frente-a-coste-total-por-plaza)).
- **No internet**: create the sandbox on `rayito-base-caps` with
  `allow_internet_access=False` ([Outbound network](../network.md)).
- **Charts**: also return `execution.results[i].png` (base64) if your model
  accepts images.

## See also { #ver-tambien }

- [MCP server](../mcp.md)
- [Agent in the sandbox](agente-en-el-sandbox.md): the agent loop running
  inside the sandbox
- [Run code](ejecutar-codigo.md)
- [Security](../security.md): what the model's code can do inside the
  sandbox
