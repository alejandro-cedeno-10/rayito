/**
 * Validación de `events` en `Sandbox.create()` (`planFeatures`): el tipo de
 * la opción y que `logging` mande los logs del sandbox a CloudWatch, que es
 * de donde el forwarder lee las líneas de evento. Espejo de
 * `rayito._lifecycle_events._options`. Pura: ninguna llamada a AWS.
 */

import { InvalidArgumentError } from "../errors.js";
import { type LoggingOption, loggingConfig } from "../sandbox/launch.js";
import { LifecycleEvents } from "./service.js";

// `loggingConfig` sólo usa el nombre de la plantilla para el log group de
// `"cloudwatch"`; aquí sólo importa qué clave resuelve, así que cualquier
// nombre sirve.
const ANY_TEMPLATE = "events-validation";

/** `true` para `"cloudwatch"` y para un `{ cloudWatch: {...} }` propio, vía
 * el mismo resolver que usa `run-microvm`; un valor inválido lanza su
 * `InvalidArgumentError`. */
export function sendsLogsToCloudWatch(logging: LoggingOption): boolean {
  return "cloudWatch" in loggingConfig(logging, ANY_TEMPLATE);
}

/** `events` debe ser un `LifecycleEvents` y `logging` debe enviar a
 * CloudWatch; si no, `InvalidArgumentError` antes de cualquier llamada a
 * AWS. */
export function validateEventsOption(events: unknown, logging: unknown): void {
  if (!(events instanceof LifecycleEvents)) {
    throw new InvalidArgumentError("events debe ser un LifecycleEvents");
  }
  if (!sendsLogsToCloudWatch((logging ?? "disabled") as LoggingOption)) {
    throw new InvalidArgumentError(
      'events necesita logging: "cloudwatch" (o { cloudWatch: {...} }): el forwarder lee ' +
        "las líneas de evento de los logs del sandbox",
    );
  }
}
