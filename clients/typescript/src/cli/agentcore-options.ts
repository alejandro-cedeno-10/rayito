/**
 * Las opciones de `--target agentcore` que se añaden a `rayito agent deploy`,
 * `invoke`, `status`, `stop` y a las órdenes nuevas `delete` y `list` (ADR-031).
 * Los nombres, tipos, valores por defecto y textos de ayuda son los de
 * `testdata/agentcore-deploy/contract.json` (y de `testdata/cli/surface.json`), los
 * mismos que la CLI de Python.
 */

import { option } from "./command.js";

export const targetOption = option(["--target"], {
  help: "Dónde se despliega: microvm (por defecto, una flota de Lambda MicroVMs) o agentcore (Amazon Bedrock AgentCore Runtime).",
  default: "microvm",
});

export const tagOption = option(["--tag"], {
  help: "Etiqueta K=V; repetible. Sólo con --target agentcore: se aplica a la pila rayito-agentcore-<nombre> (CloudFormation la propaga) y al runtime; útil si la organización exige etiquetas.",
  multiple: true,
  default: [],
});

export const bedrockModelOption = option(["--bedrock-model"], {
  help: "Id o ARN de un modelo o perfil de inferencia de Bedrock que el rol del runtime puede invocar; repetible. Sólo con --target agentcore; por defecto, el modelo del proyecto.",
  multiple: true,
  default: [],
});

export const readableSecretOption = option(["--readable-secret"], {
  help: "Nombre de un secreto de Secrets Manager que el rol del runtime puede leer; repetible. Sólo con --target agentcore; por defecto, ninguno.",
  multiple: true,
  default: [],
});

export const idleSessionTimeoutOption = option(["--idle-session-timeout"], {
  type: "int",
  help: "Segundos sin uso tras los que AgentCore termina la microVM de una sesión (60-28800; por defecto, 900). Sólo con --target agentcore.",
});

export const maxSessionLifetimeOption = option(["--max-session-lifetime"], {
  type: "int",
  help: "Segundos de vida máxima de la microVM de una sesión (60-28800; por defecto, 28800). Sólo con --target agentcore.",
});

export const stopSessionIdOption = option(["--session-id"], {
  help: "Con --target agentcore: la sesión cuya microVM se para (StopRuntimeSession).",
});

export const listTargetOption = option(["--target"], { help: "Sólo agentcore.", required: true });

export const deleteTargetOption = option(["--target"], {
  help: "Sólo agentcore: para una flota de MicroVMs usa rayito agent stop.",
  required: true,
});

export const DELETE_NAME_HELP = "Nombre del agente (el de rayito agent deploy).";

export const DELETE_YES_HELP = "No pedir confirmación (hace falta sin terminal o con --json).";

export const DELETE_HELP =
  "Borra un agente desplegado con --target agentcore: el runtime, la pila rayito-agentcore-<nombre> (repositorio ECR con sus imágenes, proyecto y logs de CodeBuild y los dos roles) y el grupo de logs del runtime. Sólo borra lo que creó Rayito para ese nombre. Pide confirmación (--yes la salta). Gratis; deja de costar el almacenamiento de las imágenes.";

export const LIST_HELP =
  "Lista los agentes desplegados con --target agentcore en la región (los runtimes que creó Rayito). Gratis: sólo lee.";
