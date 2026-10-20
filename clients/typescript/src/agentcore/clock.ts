/**
 * El reloj de las esperas de `--target agentcore`: `sleep` y `now` se inyectan
 * para que los tests recorran minutos de sondeo sin esperar. Sin imports de AWS.
 */

export type Sleep = (ms: number) => Promise<void>;

export interface ServiceSettings {
  readonly sleep?: Sleep | undefined;
  readonly now?: (() => number) | undefined;
}

const defaultSleep: Sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

export class Clock {
  readonly sleep: Sleep;
  readonly now: () => number;

  constructor(settings: ServiceSettings = {}) {
    this.sleep = settings.sleep ?? defaultSleep;
    this.now = settings.now ?? Date.now;
  }
}
