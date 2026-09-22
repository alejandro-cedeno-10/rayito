/**
 * Un sandbox de Rayito como herramienta del Vercel AI SDK, en cincuenta líneas.
 *
 * Instalación: `pnpm add ai zod rayito` (y el proveedor de tu modelo).
 * Entorno: `RAYITO_TEMPLATE` (nombre o ARN de la imagen), `AWS_REGION` y
 * `AWS_PROFILE` o las credenciales del SDK de AWS. El sandbox se crea en la
 * primera llamada, vive como mucho 900 s y se destruye al salir del proceso.
 */

import { tool } from "ai";
import { Sandbox } from "rayito";
import { z } from "zod";

const SANDBOX_TIMEOUT_MS = 900_000;
let pending: Promise<Sandbox> | undefined;

function sandbox(): Promise<Sandbox> {
  pending ??= Sandbox.create({ timeoutMs: SANDBOX_TIMEOUT_MS });
  return pending;
}

process.on("beforeExit", () => {
  void pending?.then((sbx) => sbx.kill());
});

export const runPython = tool({
  description:
    "Ejecuta código Python en un sandbox aislado con estado entre llamadas " +
    "(Linux, internet, matplotlib y pandas instalados). Devuelve text (valor de " +
    "la última expresión), stdout, stderr y error (nombre y valor si la celda " +
    "lanzó una excepción).",
  inputSchema: z.object({ code: z.string().describe("Código Python que ejecutar.") }),
  execute: async ({ code }) => {
    const execution = await (await sandbox()).runCode(code);
    return {
      text: execution.text ?? null,
      stdout: execution.logs.stdout.join(""),
      stderr: execution.logs.stderr.join(""),
      error: execution.error ? { name: execution.error.name, value: execution.error.value } : null,
    };
  },
});

// Uso: `generateText({ model, tools: { runPython }, prompt: "Calcula 2**100 en Python." })`.
