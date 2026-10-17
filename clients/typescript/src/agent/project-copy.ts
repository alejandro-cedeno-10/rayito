/**
 * `copyProject`: un `AgentProject` igual que otro salvo por los campos que se
 * cambian. `AgentProject` es inmutable y sus campos son públicos; esta es la
 * única forma de "`replace`" de TypeScript (como `dataclasses.replace` en
 * Python): copia todos los campos del proyecto, también los que añadan
 * versiones futuras, y vuelve a pasar por el constructor, que revalida todo.
 */

import { AgentProject, type AgentProjectOptions } from "./project.js";

export function copyProject(
  project: AgentProject,
  changes: Partial<AgentProjectOptions>,
): AgentProject {
  return new AgentProject({ ...project, ...changes });
}
