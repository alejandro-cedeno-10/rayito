/**
 * `Template` de E2B (m15-templates): la API JS de E2B nombra sus métodos de
 * build igual que la nativa de Rayito (`build`, `buildInBackground`,
 * `getBuildStatus`), salvo `exists` (E2B la pide estática también) y el
 * etiquetado. El shim es un subtipo delgado de `rayito.Template` que sólo
 * añade los cuatro métodos de etiquetado que 0.6 no implementa
 * (`aliasExists`/`assignTags`/`removeTags`/`getTags`): lanzan
 * `UnimplementedError` por la tabla D14, nunca `TypeError` por método
 * inexistente.
 */

import { Template as NativeTemplate } from "../templates/dsl.js";
import { unimplemented } from "./unimplemented.js";

/**
 * `rayito/e2b`'s `Template`: the same class as `rayito`'s `Template`
 * (`build`/`buildInBackground`/`getBuildStatus` cost AWS calls; the
 * builder itself does not), plus four tag stubs that never call AWS.
 *
 * Coste y activación
 * -------------------
 * Activa: `Template.build()`/`buildInBackground()`, same as the native
 *     class — see `rayito`'s `Template` for the full block.
 * Recursos y llamadas AWS: same as the native `Template`.
 * Coste aproximado: same as the native `Template`.
 * IAM: same as the native `Template` (`RayitoTemplateBuilder`).
 * Cómo apagarla: no llames a `Template.build()`/`buildInBackground()`.
 * Ejemplo:
 *     import { Template } from "rayito/e2b";
 *     const t = new Template().fromBaseImage().pipInstall(["pandas"]);
 *     await Template.build(t, "mi-template", { bucket: "<bucket>" });
 */
export class Template extends NativeTemplate {
  aliasExists(..._args: unknown[]): never {
    throw unimplemented("Template.aliasExists");
  }

  assignTags(..._args: unknown[]): never {
    throw unimplemented("Template.assignTags");
  }

  removeTags(..._args: unknown[]): never {
    throw unimplemented("Template.removeTags");
  }

  getTags(..._args: unknown[]): never {
    throw unimplemented("Template.getTags");
  }
}
