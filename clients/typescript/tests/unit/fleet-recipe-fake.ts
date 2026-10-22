/**
 * Dobles de la receta de la flota (ADR-034). Espejo de
 * `fake_fleet_recipe.py`: una tienda en memoria con las llamadas que recibió
 * y los errores que se le piden. Nunca toca AWS.
 */

import { RecipeWarning } from "../../src/agent/fleet-recipe.js";
import type { FleetRecipeStore } from "../../src/agent/fleet-recipe-aws.js";

export class MemoryRecipeStore implements FleetRecipeStore {
  readonly values = new Map<string, string>();
  readonly calls: [string, string][] = [];
  readonly failures = new Map<string, Error>();

  async save(parameter: string, value: string): Promise<void> {
    this.#record("save", parameter);
    this.values.set(parameter, value);
  }

  async load(parameter: string): Promise<string | undefined> {
    this.#record("load", parameter);
    return this.values.get(parameter);
  }

  async delete(parameter: string): Promise<void> {
    this.#record("delete", parameter);
    this.values.delete(parameter);
  }

  get operations(): string[] {
    return this.calls.map(([operation]) => operation);
  }

  #record(operation: string, parameter: string): void {
    this.calls.push([operation, parameter]);
    const failure = this.failures.get(operation);
    if (failure !== undefined) {
      throw failure;
    }
  }
}

export function denied(): RecipeWarning {
  return new RecipeWarning("recipe_access_denied");
}
