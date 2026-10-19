/**
 * Rellena las plantillas de texto con `{campo}` que comparten los mensajes de
 * `rayito setup` y de `--target agentcore`. Puro: no llama a nada.
 */

export type TemplateFields = Readonly<Record<string, string | number>>;

/** Rellena los `{campo}` de `template`; un campo sin valor es un error de programación. */
export function formatTemplate(template: string, fields: TemplateFields): string {
  return template.replace(/\{(\w+)\}/g, (_match, key: string) => {
    const value = fields[key];
    if (value === undefined) {
      throw new Error(`falta el campo ${key} del mensaje`);
    }
    return String(value);
  });
}
