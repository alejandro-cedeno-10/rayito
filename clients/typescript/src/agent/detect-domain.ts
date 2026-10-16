/**
 * `DetectOverrides`: lo que el llamante fija a mano frente a la detección de
 * `AgentProject.detect()` (ADR-029, D1). Espejo de
 * `rayito._agent._detect_domain`. Un valor puro; el orden de precedencia es
 * fichero de proyecto > argumento (`--framework`, `--language`, `--entry`,
 * `--protocol`) > detección > valor por defecto con nombre.
 */

import { InvalidArgumentError } from "../errors.js";

export interface DetectOverridesInput {
  readonly framework?: string | undefined;
  readonly language?: string | undefined;
  readonly entry?: string | undefined;
  readonly protocol?: string | undefined;
}

/** Lo que se fija sin esperar a la detección: `framework`, `language`,
 * `entry` (el objeto o la fábrica del agente) y `protocol`. `undefined` deja
 * que decida la detección. */
export class DetectOverrides {
  readonly framework: string | undefined;
  readonly language: string | undefined;
  readonly entry: string | undefined;
  readonly protocol: string | undefined;

  constructor(options: DetectOverridesInput = {}) {
    for (const label of ["framework", "language", "entry", "protocol"] as const) {
      const value = options[label];
      if (value !== undefined && (typeof value !== "string" || value === "")) {
        throw new InvalidArgumentError(
          `DetectOverrides.${label} debe ser una cadena no vacía o undefined`,
        );
      }
    }
    this.framework = options.framework;
    this.language = options.language;
    this.entry = options.entry;
    this.protocol = options.protocol;
  }
}
