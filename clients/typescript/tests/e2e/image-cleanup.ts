/**
 * Borra las imágenes MicroVM que construye un e2e (`m15-templates.e2e.test.ts`).
 * Espejo de `clients/python/tests/e2e/image_cleanup.py`.
 *
 * Cada `Template.build()` crea una imagen nueva con su versión (también
 * cuando el build falla: la imagen y su versión `FAILED` quedan en la
 * cuenta). `BuiltImages` apunta cada nombre **antes** del build y, en el
 * `afterEach`/`afterAll` (pase o falle el test), borra cada imagen con
 * `DeleteMicrovmImage`, que acepta una imagen con versiones y la pasa a
 * `DELETING` en el acto (`AWS_API_NOTES.md` §4), y espera a que desaparezca:
 *
 * - `ResourceNotFoundException`: ya no existe (el build falló antes de
 *   crearla, o el borrado terminó);
 * - `ConflictException`/`ThrottlingException`: la imagen aún está
 *   `UPDATING` o hay limitación de tasa; se reintenta con espera creciente;
 * - `DELETE_FAILED` o el plazo agotado: queda en la lista de fallos, que el
 *   test convierte en error con los nombres para borrarlos a mano.
 *
 * Necesita `lambda:DeleteMicrovmImage`. El reloj y la espera se inyectan
 * para probar la lógica sin AWS (`tests/unit/e2e-image-cleanup.test.ts`).
 */

import { DeleteMicrovmImageCommand, GetMicrovmImageCommand } from "@aws-sdk/client-lambda-microvms";
import { awsCode } from "../../src/aws/optional-client.js";

export const NOT_FOUND = "ResourceNotFoundException";
export const RETRYABLE_CODES: ReadonlySet<string> = new Set([
  "ConflictException",
  "ThrottlingException",
]);
export const RETRY_BACKOFF_MS: readonly number[] = [5_000, 10_000, 20_000, 40_000, 80_000];
export const POLL_INTERVAL_MS = 5_000;
export const DEFAULT_TIMEOUT_MS = 600_000;
const DELETED_STATE = "DELETED";
const DELETE_FAILED_STATE = "DELETE_FAILED";

/** Lo mínimo de `LambdaMicrovmsClient` que hace falta (`controlPlane.client`). */
export interface MicrovmsSender {
  send(command: unknown): Promise<unknown>;
}

export interface BuiltImagesOptions {
  readonly client: MicrovmsSender;
  readonly resolveArn: (name: string) => Promise<string>;
  readonly sleep?: (ms: number) => Promise<void>;
  readonly now?: () => number;
  readonly timeoutMs?: number;
}

export class BuiltImages {
  readonly names: string[] = [];
  readonly #client: MicrovmsSender;
  readonly #resolveArn: (name: string) => Promise<string>;
  readonly #sleep: (ms: number) => Promise<void>;
  readonly #now: () => number;
  readonly #timeoutMs: number;

  constructor(options: BuiltImagesOptions) {
    this.#client = options.client;
    this.#resolveArn = options.resolveArn;
    this.#sleep = options.sleep ?? ((ms) => new Promise((resolve) => setTimeout(resolve, ms)));
    this.#now = options.now ?? Date.now;
    this.#timeoutMs = options.timeoutMs ?? DEFAULT_TIMEOUT_MS;
  }

  /** Apunta `name` para borrarlo al final; se llama antes del build. */
  track(name: string): string {
    this.names.push(name);
    return name;
  }

  /** Borra y olvida cada imagen apuntada; devuelve `nombre: motivo` de las
   * que no se pudieron borrar (vacío si todo quedó limpio). */
  async deleteAll(): Promise<string[]> {
    const failures: string[] = [];
    for (const name of this.names.splice(0)) {
      const reason = await this.delete(await this.#resolveArn(name));
      if (reason !== undefined) {
        failures.push(`${name}: ${reason}`);
      }
    }
    return failures;
  }

  /** `undefined` cuando la imagen ya no existe; si no, el motivo. */
  async delete(arn: string): Promise<string | undefined> {
    const retries = RETRY_BACKOFF_MS[Symbol.iterator]();
    for (;;) {
      try {
        await this.#client.send(new DeleteMicrovmImageCommand({ imageIdentifier: arn }));
      } catch (error) {
        const code = awsCode(error) ?? "Unknown";
        if (code === NOT_FOUND) {
          return undefined;
        }
        const backoff = retries.next();
        if (!RETRYABLE_CODES.has(code) || backoff.done === true) {
          return code;
        }
        await this.#sleep(backoff.value);
        continue;
      }
      return this.waitUntilGone(arn);
    }
  }

  async waitUntilGone(arn: string): Promise<string | undefined> {
    const started = this.#now();
    for (;;) {
      let state: string;
      try {
        const response = (await this.#client.send(
          new GetMicrovmImageCommand({ imageIdentifier: arn }),
        )) as { state?: string };
        state = String(response.state);
      } catch (error) {
        const code = awsCode(error) ?? "Unknown";
        return code === NOT_FOUND ? undefined : code;
      }
      if (state === DELETED_STATE) {
        return undefined;
      }
      if (state === DELETE_FAILED_STATE) {
        return state;
      }
      if (this.#now() - started >= this.#timeoutMs) {
        return `sigue ${state} tras ${Math.round(this.#timeoutMs / 1000)} s`;
      }
      await this.#sleep(POLL_INTERVAL_MS);
    }
  }
}
