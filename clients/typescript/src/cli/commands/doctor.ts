/**
 * `rayito doctor`: diez comprobaciones de la cuenta antes del primer
 * `Sandbox.create()`, con `OK | WARN | FAIL | SKIP` y salida 1 sólo con `FAIL`.
 */

import { defineCommand, flag, option } from "../command.js";
import { runDoctor } from "../doctor/checks.js";
import { BUCKET_ENV_VAR, DEFAULT_TEMPLATE, LAUNCH_TIMEOUT_SECONDS } from "../doctor/constants.js";
import { newDoctorContext } from "../doctor/model.js";
import { exitCodeFor, renderHuman, renderJson } from "../doctor/render.js";

/** Lo que cuesta `--launch` en la ayuda: un sandbox de `LAUNCH_TIMEOUT_SECONDS` s. */
const LAUNCH_COST_HINT = "≈ $0,002";

export const doctorCommand = defineCommand({
  path: ["doctor"],
  help:
    "Diagnostica la cuenta antes del primer `Sandbox.create()`: credenciales, imágenes " +
    "gestionadas, cuotas, IAM, bucket, gate de la imagen, MicroVMs vivos, token, agente y " +
    "compatibilidad SDK ↔ rayd ↔ imagen. Con --efs-vpc-id, además, si esa VPC existente sirve " +
    "para volúmenes EFS (sin crear nada).",
  parameters: [
    option(["--template"], {
      default: DEFAULT_TEMPLATE,
      help: "Nombre o ARN de la imagen a comprobar.",
    }),
    option(["--template-version"], {
      help: "Versión de la imagen; por defecto la última activa.",
    }),
    option(["--bucket"], { help: `Bucket de artefactos (o ${BUCKET_ENV_VAR}).` }),
    flag(["--launch"], {
      help:
        `Crea un sandbox de ${LAUNCH_TIMEOUT_SECONDS} s (${LAUNCH_COST_HINT}) para las ` +
        "comprobaciones 8-10 y lo mata al final.",
    }),
    option(["--efs-vpc-id"], {
      help:
        "Comprueba (sólo lectura) si esta VPC existente sirve para " +
        "`rayito stack deploy efs-volumes`; con --efs-subnet-ids.",
    }),
    option(["--efs-subnet-ids"], {
      help: "De 1 a 3 subredes de --efs-vpc-id, separadas por comas, cada una en otra AZ.",
    }),
  ],
  run: async (context) => {
    const { values } = context;
    const services = await context.services();
    const doctorContext = newDoctorContext({
      template: values.string("template") ?? DEFAULT_TEMPLATE,
      templateVersion: values.string("template_version"),
      bucket: values.string("bucket") ?? (context.env[BUCKET_ENV_VAR] || undefined),
      launch: values.flag("launch"),
      efsVpcId: values.string("efs_vpc_id"),
      efsSubnetIds: values.string("efs_subnet_ids"),
    });
    const results = await runDoctor(services.doctor, doctorContext, (message) =>
      context.console.echo(message, { err: true }),
    );
    const exitCode = exitCodeFor(results);
    if (context.globals.json) {
      renderJson(context.console, {
        region: services.region,
        context: doctorContext,
        results,
        exitCode,
      });
    } else {
      renderHuman(context.console, results);
      if (doctorContext.launched !== undefined) {
        context.console.echo(`sandbox de --launch terminado: ${doctorContext.launched.sandboxId}`);
      }
    }
    return exitCode === 0 ? undefined : exitCode;
  },
});
