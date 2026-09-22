/**
 * Logger opcional del SDK (por defecto silencio). Sólo se registran ids de
 * sandbox, estados, generaciones, duraciones, bytes, clases de motivo, pids,
 * rutas de watch e ids de contexto: nunca salida, bytes de PTY, ficheros,
 * código, envs, tokens, `runHookPayload` ni cabeceras.
 */
export interface Logger {
  debug?(message: string, fields?: Readonly<Record<string, unknown>>): void;
  info?(message: string, fields?: Readonly<Record<string, unknown>>): void;
  warn?(message: string, fields?: Readonly<Record<string, unknown>>): void;
  error?(message: string, fields?: Readonly<Record<string, unknown>>): void;
}
