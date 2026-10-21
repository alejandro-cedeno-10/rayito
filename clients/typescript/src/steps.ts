/**
 * Un solo código para las rutas síncrona y asíncrona (agentcore-cdk, ADR-032
 * D7). La lógica que lee del disco se escribe una vez como generador que pide
 * cada lectura con `yield` (una petición del puerto de quien la usa); un
 * conductor la ejecuta contra un adaptador síncrono (los constructos CDK,
 * cuyos constructores no pueden esperar) o asíncrono (el SDK). Los errores
 * del adaptador vuelven al generador en el mismo punto, así que un
 * `try`/`catch` del dominio se comporta igual en las dos rutas.
 */

/** Los pasos de un cálculo que pide lecturas `R` y termina con `T`. */
export type Steps<R, T> = Generator<R, T, unknown>;

/** Ejecuta `steps` respondiendo cada petición con `answer`, sin esperar nada. */
export function runSteps<R, T>(steps: Steps<R, T>, answer: (request: R) => unknown): T {
  let next = steps.next();
  while (next.done !== true) {
    let reply: unknown;
    try {
      reply = answer(next.value);
    } catch (error) {
      next = steps.throw(error);
      continue;
    }
    next = steps.next(reply);
  }
  return next.value;
}

/** Ejecuta `steps` respondiendo cada petición con la promesa de `answer`. */
export async function runStepsAsync<R, T>(
  steps: Steps<R, T>,
  answer: (request: R) => Promise<unknown>,
): Promise<T> {
  let next = steps.next();
  while (next.done !== true) {
    let reply: unknown;
    try {
      reply = await answer(next.value);
    } catch (error) {
      next = steps.throw(error);
      continue;
    }
    next = steps.next(reply);
  }
  return next.value;
}
