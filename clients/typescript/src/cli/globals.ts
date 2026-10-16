/**
 * Las opciones globales de la raíz: `--profile`, `--region`, `--json`,
 * `--verbose` y `--version`. Van antes del primer subcomando, como en la CLI
 * de Python.
 */

import { flag, type GlobalOptions, option, type ParameterSpec } from "./command.js";

export const PROFILE_FLAG = "--profile";
export const REGION_FLAG = "--region";
export const JSON_FLAG = "--json";
export const VERBOSE_FLAG = "--verbose";
export const VERSION_FLAG = "--version";
export const HELP_FLAG = "--help";

export const ROOT_HELP = "Rayito: sandboxes sobre AWS Lambda MicroVMs";

export const ROOT_PARAMETERS: readonly ParameterSpec[] = [
  option([PROFILE_FLAG], { help: "Perfil de AWS (por defecto AWS_PROFILE)." }),
  option([REGION_FLAG], { help: "Región de AWS (por defecto AWS_REGION)." }),
  flag([JSON_FLAG], {
    name: "json_output",
    help: "Un único documento JSON por stdout; progreso por stderr.",
  }),
  flag([VERBOSE_FLAG], { help: "Logs INFO del SDK y de la CLI." }),
  flag([VERSION_FLAG], { help: "Versión y runtime (node)." }),
];

export const NO_GLOBALS: GlobalOptions = {
  profile: undefined,
  region: undefined,
  json: false,
  verbose: false,
};
