## ADDED Requirements

### Requirement: LangChain and Vercel AI SDK adapters as examples
The repository SHALL contain `docs/examples/langchain_tool.py` and `docs/examples/vercel_ai_tool.ts`, each at most 50 lines, as copy-paste examples and not as packages, test dependencies or CI artefacts. `langchain_tool.py` SHALL define a LangChain tool with `from langchain.tools import tool` (`@tool def run_python(code: str) -> str`) over the public sync SDK (`rayito.Sandbox.create(timeout=900)`, template from `RAYITO_TEMPLATE`), create the sandbox lazily on the first call, kill it with `atexit`, return a text summary with the execution's `text`, `stdout`, `stderr` and `error`, show `create_agent(model, tools=[run_python])` under `if __name__ == "__main__":`, and SHALL NOT import `rayito.mcp` or any `rayito._*` module. `vercel_ai_tool.ts` SHALL export `runPython` built with `tool({ description, inputSchema: z.object({ code: z.string() }), execute })` from `ai` and `zod` over the public TypeScript SDK (`Sandbox.create({ timeoutMs: 900_000 })`, `runCode`), create the sandbox lazily, kill it on `beforeExit`, and return `{ text, stdout, stderr, error }`. Both files SHALL start with a Spanish header stating the install command (`pip install rayito langchain` / `pnpm add ai zod rayito`) and the environment variables. `python -m py_compile` and `uvx ruff check` SHALL pass on the Python file; the TypeScript file SHALL type-check with `tsc --noEmit --strict` in a scratch project that has `ai`, `zod` and the local `rayito` package installed, with the command recorded in the change's `tasks.md` notes.

#### Scenario: python example is valid
- **WHEN** `python -m py_compile docs/examples/langchain_tool.py` and `uvx ruff check docs/examples/langchain_tool.py` run
- **THEN** both exit 0 and the file has at most 50 lines

#### Scenario: typescript example is valid
- **WHEN** `pnpm exec tsc --noEmit --strict --module nodenext --moduleResolution nodenext --target es2022 vercel_ai_tool.ts` runs in a scratch project with `ai`, `zod` and `file:<repo>/clients/typescript` installed
- **THEN** it exits 0 and the file has at most 50 lines

#### Scenario: examples reference only public surfaces
- **WHEN** both files are grepped for `rayito.mcp`, `rayito._` and `rayito/src`
- **THEN** there are no matches
