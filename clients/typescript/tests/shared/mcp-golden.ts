/**
 * El golden compartido `testdata/mcp/tools.json` y cómo se obtiene de un
 * servidor MCP vivo (nombre, instrucciones y, de cada herramienta, descripción,
 * anotaciones, argumentos y campos de salida), el mismo que comprueba el
 * servidor de Python (`test_mcp_contract.py`). Lo usan el test de contrato en
 * proceso y el e2e local por stdio.
 */

import { readFileSync } from "node:fs";
import { join } from "node:path";
import type { Client } from "@modelcontextprotocol/client";

interface JsonSchemaProperty {
  readonly type?: string;
  readonly anyOf?: ReadonlyArray<{ readonly type?: string }>;
  readonly description?: string;
  readonly default?: unknown;
  readonly minimum?: number;
  readonly maximum?: number;
}

interface JsonSchema {
  readonly properties?: Record<string, JsonSchemaProperty>;
  readonly required?: readonly string[];
}

interface ListedTool {
  readonly name: string;
  readonly description?: string;
  readonly annotations?: Record<string, unknown>;
  readonly inputSchema: JsonSchema;
  readonly outputSchema?: JsonSchema;
}

export const GOLDEN = JSON.parse(
  readFileSync(join(import.meta.dirname, "../../../../testdata/mcp/tools.json"), "utf8"),
) as { server: { name: string; instructions: string }; tools: unknown[] };

const NULL_TYPE = "null";
const CONSTRAINTS = ["minimum", "maximum"] as const;

function collapse(text: string | undefined): string {
  return (text ?? "").split(/\s+/).filter(Boolean).join(" ");
}

function propertyType(spec: JsonSchemaProperty): string | undefined {
  return spec.type ?? spec.anyOf?.find((option) => option.type !== NULL_TYPE)?.type;
}

function describeInput(schema: JsonSchema): Array<Record<string, unknown>> {
  const required = new Set(schema.required ?? []);
  return Object.entries(schema.properties ?? {}).map(([name, spec]) => {
    const entry: Record<string, unknown> = {
      name,
      type: propertyType(spec),
      required: required.has(name),
      description: spec.description,
    };
    if (spec.default !== undefined && spec.default !== null) {
      entry.default = spec.default;
    }
    for (const constraint of CONSTRAINTS) {
      if (spec[constraint] !== undefined) {
        entry[constraint] = spec[constraint];
      }
    }
    return entry;
  });
}

function describeTool(tool: ListedTool): Record<string, unknown> {
  return {
    name: tool.name,
    description: collapse(tool.description),
    annotations: tool.annotations ?? {},
    input: describeInput(tool.inputSchema),
    output:
      tool.outputSchema === undefined
        ? null
        : Object.keys(tool.outputSchema.properties ?? {}).sort(),
  };
}

/** Lo que el golden describe, leído de un cliente MCP ya conectado. */
export async function describeServer(client: Client): Promise<Record<string, unknown>> {
  const listed = (await client.listTools()).tools as unknown as ListedTool[];
  return {
    server: {
      name: client.getServerVersion()?.name,
      instructions: collapse(client.getInstructions()),
    },
    tools: listed.map(describeTool),
  };
}
