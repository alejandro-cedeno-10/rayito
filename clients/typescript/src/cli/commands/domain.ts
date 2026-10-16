/**
 * `rayito domain deploy | status | destroy` (m15-custom-domain, ADR-024): una
 * fachada fina sobre `rayito stack {deploy,status,destroy} custom-domain` con
 * los nombres de parámetro propios de `CustomDomain` (`--public-domain`,
 * `--certificate-arn`). Para parámetros avanzados o un nombre de pila distinto
 * usa `rayito stack` directamente.
 *
 * `status` y `destroy` sólo necesitan el nombre de la pila: van directas a
 * `OptionalStacks`, nunca pasan por `CustomDomain`, así que no piden
 * `--public-domain`. `deploy` sí construye una `CustomDomain` real, porque
 * valida el formato de `--public-domain` antes de tocar AWS.
 */

import { WILDCARD_LABEL } from "../../custom-domain/domain.js";
import {
  CUSTOM_DOMAIN_WAIT_TIMEOUT_MS,
  DISTRIBUTION_DOMAIN_NAME_OUTPUT_KEY,
  STACK_COMPONENT,
} from "../../custom-domain/service.js";
import { COMPONENT } from "../../stacks/components/custom-domain.js";
import { type CommandContext, defineCommand, defineGroup, flag, option } from "../command.js";
import { EXIT_FAILURE } from "../exit-codes.js";

export const domainGroup = defineGroup({
  path: ["domain"],
  help: "Dominio propio sobre CloudFront (experimental, CustomDomain): deploy, status, destroy.",
});

const stackNameOption = option(["--stack-name"], { help: "Nombre de la pila." });
const yesFlag = flag(["--yes"], { help: "No pedir confirmación." });

function printCost(context: CommandContext): void {
  const { cost } = COMPONENT;
  const echo = (line: string): void => context.console.echo(line);
  echo(`Coste y activación de ${STACK_COMPONENT}`);
  echo(`  Recursos y llamadas AWS: ${cost.creates.join(", ")}`);
  echo(`  Coste aproximado (reposo): ${cost.idleMonthly}`);
  if ((cost.perUse ?? []).length > 0) {
    echo(`  Coste por uso: ${(cost.perUse ?? []).join("; ")}`);
  }
  echo(`  Cómo apagarla: rayito domain destroy (${cost.removal})`);
}

export const deployCommand = defineCommand({
  path: ["domain", "deploy"],
  help: "Despliega (o actualiza) la pila custom-domain; imprime su coste y el CNAME.",
  parameters: [
    option(["--public-domain"], {
      required: true,
      help: "Dominio público (p. ej. sbx.example.com).",
    }),
    option(["--certificate-arn"], { required: true, help: "Certificado ACM, en us-east-1." }),
    stackNameOption,
    option(["--alternate-domain-name"], {
      name: "alternate_domain_names",
      multiple: true,
      help:
        "Nombre alternativo explícito (repetible) en lugar del comodín *.<public-domain>; " +
        "p. ej. 8000-<alias>.<public-domain>.",
    }),
    yesFlag,
  ],
  run: async (context) => {
    const { values } = context;
    const services = await context.services();
    const alternateNames = values.strings("alternate_domain_names");
    const domain = services.customDomain({
      publicDomain: values.string("public_domain") ?? "",
      stackName: values.string("stack_name"),
    });
    if (!context.globals.json) {
      printCost(context);
    }
    if (
      !values.flag("yes") &&
      !context.globals.json &&
      !(await context.console.confirm("¿Desplegar esta pila?"))
    ) {
      return EXIT_FAILURE;
    }
    const status = await domain.deploy({
      certificateArn: values.string("certificate_arn") ?? "",
      alternateDomainNames: alternateNames.length === 0 ? undefined : alternateNames,
    });
    if (context.globals.json) {
      context.console.emitJson({ name: status.name, state: status.state, outputs: status.outputs });
      return undefined;
    }
    context.console.echo(`${STACK_COMPONENT}: ${status.state}`);
    const target = status.outputs[DISTRIBUTION_DOMAIN_NAME_OUTPUT_KEY];
    if (target) {
      const names =
        alternateNames.length === 0 ? [`${WILDCARD_LABEL}.${domain.publicDomain}`] : alternateNames;
      context.console.echo(`  DNS: apunta un CNAME/alias de ${names.join(", ")} a ${target}`);
    }
    return undefined;
  },
});

export const statusCommand = defineCommand({
  path: ["domain", "status"],
  help: "Estado y salidas de la pila custom-domain (sólo lectura).",
  parameters: [stackNameOption],
  run: async (context) => {
    const { stacks } = await context.services();
    const stackName = context.values.string("stack_name");
    const status = await stacks.status(
      STACK_COMPONENT,
      stackName === undefined ? {} : { stackName },
    );
    if (context.globals.json) {
      context.console.emitJson(
        status === undefined
          ? null
          : { name: status.name, state: status.state, outputs: status.outputs },
      );
      return undefined;
    }
    if (status === undefined) {
      context.console.echo(`${STACK_COMPONENT}: no desplegado`);
      return undefined;
    }
    context.console.echo(`${STACK_COMPONENT}: ${status.state}`);
    for (const [key, value] of Object.entries(status.outputs)) {
      context.console.echo(`  ${key} = ${value}`);
    }
    return undefined;
  },
});

export const destroyCommand = defineCommand({
  path: ["domain", "destroy"],
  help: "Borra la pila custom-domain; dice antes qué se conserva.",
  parameters: [stackNameOption, yesFlag],
  run: async (context) => {
    if (!context.globals.json) {
      context.console.echo(`Al borrar: ${COMPONENT.cost.removal}`);
    }
    if (
      !context.values.flag("yes") &&
      !context.globals.json &&
      !(await context.console.confirm("¿Borrar la pila de dominio propio?"))
    ) {
      return EXIT_FAILURE;
    }
    const { stacks } = await context.services();
    const stackName = context.values.string("stack_name");
    await stacks.destroy(STACK_COMPONENT, {
      ...(stackName === undefined ? {} : { stackName }),
      waitTimeoutMs: CUSTOM_DOMAIN_WAIT_TIMEOUT_MS,
    });
    if (context.globals.json) {
      context.console.emitJson({ name: STACK_COMPONENT, state: "destroyed" });
      return undefined;
    }
    context.console.echo(`${STACK_COMPONENT}: borrado`);
    return undefined;
  },
});
