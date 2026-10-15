// Estado que vive mientras la pestaña está abierta y se comparte entre vistas:
// las muestras de las gráficas del resumen y el último coste facturado. Nada
// se guarda en el navegador: al recargar, se vuelve a empezar.

/** Tope de muestras: con el sondeo de 15 s, unos 30 min de historia. */
export const HISTORY_MAX_SAMPLES = 120;

const samples = [];
const billing = { data: null };

/** Añade una muestra `{ at, running, suspended, costUsd }` y descarta las más viejas. */
export function recordSample(sample) {
  samples.push(sample);
  if (samples.length > HISTORY_MAX_SAMPLES) samples.splice(0, samples.length - HISTORY_MAX_SAMPLES);
}

export function history() {
  return samples.slice();
}

export function lastBilled() {
  return billing.data;
}

export function rememberBilled(data) {
  billing.data = data;
}
