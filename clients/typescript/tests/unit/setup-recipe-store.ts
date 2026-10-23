/**
 * Ningún test unitario llega a Parameter Store: la tienda de recetas por
 * defecto de la flota (ADR-034) es una vacía en memoria por llamada. Los tests
 * de la receta pasan la suya (`recipeStore`) o prueban `SsmRecipeStore` con un
 * cliente de doble. Espejo del `isolated_recipe_store` del `conftest.py`.
 */

import { vi } from "vitest";
import { MemoryRecipeStore } from "./fleet-recipe-fake.js";

vi.mock("../../src/agent/fleet-recipe-aws.js", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../../src/agent/fleet-recipe-aws.js")>()),
  defaultRecipeStore: () => new MemoryRecipeStore(),
}));
