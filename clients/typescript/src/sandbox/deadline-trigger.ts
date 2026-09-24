/**
 * El temporizador del disparador del modo `pause` (ADR-011): uno solo por
 * sandbox, rearmable y `unref()` para que nunca retenga el proceso de Node.
 * Rearmar sustituye al anterior; `cancel()` es idempotente. Qué hace al
 * disparar lo decide `SandboxCore`.
 */
export class DeadlineTrigger {
  readonly #fire: () => Promise<void>;
  #timer: ReturnType<typeof setTimeout> | undefined;

  constructor(fire: () => Promise<void>) {
    this.#fire = fire;
  }

  get armed(): boolean {
    return this.#timer !== undefined;
  }

  /** `undefined` sólo cancela: el plazo leído no pide disparador. */
  arm(delayMs: number | undefined): void {
    this.cancel();
    if (delayMs === undefined) {
      return;
    }
    const timer = setTimeout(() => {
      this.#timer = undefined;
      void this.#fire();
    }, delayMs);
    timer.unref();
    this.#timer = timer;
  }

  cancel(): void {
    if (this.#timer !== undefined) {
      clearTimeout(this.#timer);
      this.#timer = undefined;
    }
  }
}
