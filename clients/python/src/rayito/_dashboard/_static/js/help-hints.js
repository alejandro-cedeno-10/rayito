// Qué está activado y cómo activarlo, según dónde corre el panel. Es lógica
// pura (sin DOM) para poder probarla con Node: en el panel local las opciones
// se dan por la CLI; en el de tu cuenta salen de la pila y del grupo de
// Cognito del usuario, y esas banderas no existen.

export const DEPLOYMENT_STACK = "stack";

const LOCAL_HINTS = {
  actions: "--allow-actions",
  costExplorer: "--cost-explorer",
  events: "--events-stack <nombre>",
};

const STACK_HINTS = {
  actions: "pide a un administrador que te añada al grupo de acciones",
  costExplorer: "se activa al desplegar la pila con EnableCostExplorer=true",
  events: "no está disponible en el panel de tu cuenta; usa el panel local con --events-stack",
};

/** Las tres filas de «Qué está activado ahora»: etiqueta, estado y pista si está apagado. */
export function statusRows(meta) {
  const isStack = meta.deployment === DEPLOYMENT_STACK;
  const hints = isStack ? STACK_HINTS : LOCAL_HINTS;
  const rows = [
    { label: "Acciones (matar, suspender, reanudar)", enabled: meta.allowActions, hint: hints.actions },
    { label: "Coste facturado", enabled: meta.costExplorer.enabled, hint: hints.costExplorer },
    { label: "Eventos", enabled: meta.eventsConfigured, hint: hints.events },
  ];
  return rows.map((row) => ({ ...row, hintIsFlag: !isStack }));
}
