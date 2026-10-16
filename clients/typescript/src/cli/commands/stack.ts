/**
 * `rayito stack list | status | deploy | destroy`: la CLI del convenio
 * `OptionalStack` (ADR-016). `deploy` imprime siempre el `CostStatement` del
 * componente y pide confirmación salvo `--yes`; `destroy` dice qué se
 * conserva; `list` no hace ninguna llamada a AWS.
 */

import { UnimplementedError } from "../../errors.js";
import type { ParameterChange, StackComponent } from "../../stacks/model.js";
import { COMPONENTS } from "../../stacks/registry.js";
import type { OptionalStacks } from "../../stacks/service.js";
import { pythonStringRepr } from "../../strings.js";
import {
  argument,
  type CommandContext,
  defineCommand,
  defineGroup,
  flag,
  option,
} from "../command.js";
import { EXIT_FAILURE } from "../exit-codes.js";
import { UsageError } from "../failures.js";
import { pairsOrUsage } from "./sandbox.js";

/** Lo que se imprime en lugar del valor de un parámetro `NoEcho`. */
export const HIDDEN_VALUE = "(oculto)";
/** Cómo se imprime "la pila todavía no tiene este parámetro". */
export const ABSENT_VALUE = "(sin valor)";
/** El `removal` de un componente que no lo declara (`CostStatement.removal` de Python). */
export const DEFAULT_REMOVAL = "destroy() borra todo lo que esta pila creó";
const NO_RESOURCES = "(ninguno)";

export const stackGroup = defineGroup({
  path: ["stack"],
  help: "Componentes OptionalStack en tu propia cuenta: list, deploy, status, destroy.",
});

const stackNameOption = option(["--stack-name"], {
  help: "Nombre de la pila (por defecto rayito-<componente>).",
});
const yesFlag = flag(["--yes"], { help: "No pedir confirmación." });

function resolveComponent(stacks: OptionalStacks, name: string): StackComponent {
  const found = stacks.components().find((candidate) => candidate.name === name);
  if (found === undefined) {
    throw new UsageError(`component: componente desconocido: ${pythonStringRepr(name)}`);
  }
  return found;
}

function printCost(context: CommandContext, component: StackComponent): void {
  const cost = component.cost;
  const print = (line: string): void => context.console.echo(line);
  print(`Coste y activación de ${component.name}`);
  print(`  Activa: rayito stack deploy ${component.name}`);
  print(`  Recursos y llamadas AWS: ${cost.creates.join(", ") || NO_RESOURCES}`);
  print(`  Coste aproximado (reposo): ${cost.idleMonthly}`);
  if ((cost.perUse ?? []).length > 0) {
    print(`  Coste por uso: ${(cost.perUse ?? []).join("; ")}`);
  }
  print(
    `  Cómo apagarla: rayito stack destroy ${component.name} (${cost.removal ?? DEFAULT_REMOVAL})`,
  );
  if (cost.source) {
    print(`  Fuente: ${cost.source}`);
  }
}

function printChanges(
  context: CommandContext,
  component: StackComponent,
  changes: readonly ParameterChange[],
): void {
  const hidden = new Set(
    (component.parameters ?? []).filter((parameter) => parameter.noEcho).map(({ name }) => name),
  );
  if (changes.length === 0) {
    context.console.echo("  Parámetros: sin cambios (los no pasados conservan su valor actual)");
    return;
  }
  context.console.echo("  Parámetros que cambian (los no pasados conservan su valor actual):");
  for (const change of changes) {
    const concealed = hidden.has(change.name);
    const before = concealed ? HIDDEN_VALUE : (change.before ?? ABSENT_VALUE);
    const after = concealed ? HIDDEN_VALUE : change.after;
    context.console.echo(
      `    ${change.name}: ${pythonStringRepr(before)} -> ${pythonStringRepr(after)}`,
    );
  }
}

/**
 * Imprime el `CostStatement` (y, si se pasan, los parámetros que cambian) y
 * pide confirmación salvo con `--yes` o `--json`; `false` si se declina.
 */
export async function confirmDeploy(
  context: CommandContext,
  component: StackComponent,
  changes: readonly ParameterChange[] | undefined,
): Promise<boolean> {
  if (!context.globals.json) {
    printCost(context, component);
    if (changes !== undefined) {
      printChanges(context, component, changes);
    }
  }
  if (context.values.flag("yes") || context.globals.json) {
    return true;
  }
  return context.console.confirm("¿Desplegar esta pila?");
}

export async function confirmDestroy(
  context: CommandContext,
  component: StackComponent,
): Promise<boolean> {
  if (!context.globals.json) {
    context.console.echo(`Al borrar: ${component.cost.removal ?? DEFAULT_REMOVAL}`);
  }
  if (context.values.flag("yes") || context.globals.json) {
    return true;
  }
  return context.console.confirm(`¿Borrar la pila de ${component.name}?`);
}

export const listCommand = defineCommand({
  path: ["stack", "list"],
  help: "El catálogo completo; nunca hace una llamada a AWS ni pide región.",
  parameters: [],
  run: async (context) => {
    const components = COMPONENTS;
    if (context.globals.json) {
      context.console.emitJson(
        components.map((component) => ({
          name: component.name,
          supported: component.supported ?? true,
          idleMonthly: component.cost.idleMonthly,
          description: component.description,
        })),
      );
      return undefined;
    }
    context.console.table(
      ["name", "supported", "idle_monthly"],
      components.map((component) => [
        component.name,
        component.supported ?? true,
        component.cost.idleMonthly,
      ]),
    );
    return undefined;
  },
});

export const statusCommand = defineCommand({
  path: ["stack", "status"],
  help: "Estado y salidas de la pila del componente (sólo DescribeStacks).",
  parameters: [
    argument("component", { required: true, help: "Nombre del componente." }),
    stackNameOption,
  ],
  run: async (context) => {
    const { stacks } = await context.services();
    const name = context.values.string("component") ?? "";
    resolveComponent(stacks, name);
    const stackName = context.values.string("stack_name");
    const status = await stacks.status(name, stackName === undefined ? {} : { stackName });
    if (context.globals.json) {
      context.console.emitJson(
        status === undefined
          ? null
          : { name: status.name, state: status.state, outputs: status.outputs },
      );
      return undefined;
    }
    if (status === undefined) {
      context.console.echo(`${name}: no desplegado`);
      return undefined;
    }
    context.console.echo(`${name}: ${status.state}`);
    for (const [key, value] of Object.entries(status.outputs)) {
      context.console.echo(`  ${key} = ${value}`);
    }
    return undefined;
  },
});

export const deployCommand = defineCommand({
  path: ["stack", "deploy"],
  help: "Crea o actualiza la pila; imprime coste y parámetros que cambian.",
  parameters: [
    argument("component", { required: true, help: "Nombre del componente." }),
    stackNameOption,
    option(["--param"], { multiple: true, default: [], help: "Parámetro K=V; repetible." }),
    option(["--artifact-bucket"], {
      help: "Bucket donde se sube el código Lambda del componente.",
    }),
    option(["--tag"], { multiple: true, default: [], help: "Etiqueta K=V; repetible." }),
    yesFlag,
  ],
  run: async (context) => {
    const { values } = context;
    const { stacks } = await context.services();
    const name = values.string("component") ?? "";
    const resolved = resolveComponent(stacks, name);
    if (resolved.supported === false) {
      if (!context.globals.json) {
        printCost(context, resolved);
      }
      throw new UnimplementedError(`rayito stack deploy ${name}`, resolved.description);
    }
    const parameters = pairsOrUsage(values.strings("param"), "--param");
    const tags = pairsOrUsage(values.strings("tag"), "--tag");
    const stackName = values.string("stack_name");
    const artifactBucket = values.string("artifact_bucket");
    const target = {
      ...(stackName === undefined ? {} : { stackName }),
      ...(artifactBucket === undefined ? {} : { artifactBucket }),
    };
    const changes = context.globals.json
      ? undefined
      : await stacks.parameterChanges(name, { ...target, parameters });
    if (!(await confirmDeploy(context, resolved, changes))) {
      return EXIT_FAILURE;
    }
    const status = await stacks.deploy(name, { ...target, parameters, tags });
    if (context.globals.json) {
      context.console.emitJson({ name: status.name, state: status.state, outputs: status.outputs });
      return undefined;
    }
    context.console.echo(`${name}: ${status.state}`);
    return undefined;
  },
});

export const destroyCommand = defineCommand({
  path: ["stack", "destroy"],
  help: "Borra la pila del componente; dice antes qué se conserva.",
  parameters: [
    argument("component", { required: true, help: "Nombre del componente." }),
    stackNameOption,
    yesFlag,
  ],
  run: async (context) => {
    const { stacks } = await context.services();
    const name = context.values.string("component") ?? "";
    const resolved = resolveComponent(stacks, name);
    if (!(await confirmDestroy(context, resolved))) {
      return EXIT_FAILURE;
    }
    const stackName = context.values.string("stack_name");
    await stacks.destroy(name, stackName === undefined ? {} : { stackName });
    if (context.globals.json) {
      context.console.emitJson({ name, state: "destroyed" });
      return undefined;
    }
    context.console.echo(`${name}: borrado`);
    return undefined;
  },
});
