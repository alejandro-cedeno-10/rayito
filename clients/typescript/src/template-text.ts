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

/** Los valores de un mensaje, todos como texto (igual que `fields` en Python). */
export type MessageFields = Readonly<Record<string, string>>;

/**
 * `fields` con cada valor pasado a texto. Python guarda `str(valor)` en el
 * `fields` de sus errores, así que `max: 64` tiene que salir `"64"` en los
 * dos SDK para que los casos del contrato comparen igual.
 */
export function messageFields(fields: TemplateFields): MessageFields {
  return Object.fromEntries(Object.entries(fields).map(([key, value]) => [key, String(value)]));
}
