/**
 * `sandbox.files` de E2B JS 2.51: el `Filesystem` nativo con la firma de E2B
 * en `watchDir(path, onEvent, opts)`. El resto de métodos (`read` con
 * `format: "blob"`, `write`, `writeFiles`, `uploadUrl`...) son los nativos.
 */

import { errorMessage } from "../errors.js";
import type { FilesystemEvent } from "../models.js";
import type { ExitCallback, WatchHandle, WatchOptions } from "../sandbox/filesystem.js";
import { Filesystem as NativeFilesystem } from "../sandbox/filesystem.js";
import { settleAsyncCallback } from "./compat.js";

/** El deadline del stream de `watchDir` cuando falta `timeoutMs`, como en E2B. */
export const DEFAULT_WATCH_TIMEOUT_MS = 60_000;

export type WatchEventCallback = (event: FilesystemEvent) => void | Promise<void>;

/**
 * `allowNetworkMounts` se acepta y no cambia nada: el sandbox no tiene
 * montajes de red. Un watch abierto cruza el endpoint con keepalives y cuenta
 * como actividad para la política de idle.
 */
export interface WatchOpts {
  readonly onExit?: ExitCallback | undefined;
  readonly recursive?: boolean | undefined;
  readonly includeEntry?: boolean | undefined;
  readonly allowNetworkMounts?: boolean | undefined;
  readonly user?: string | undefined;
  /** Deadline del stream en ms (60 000 por defecto; `0` = sin deadline). */
  readonly timeoutMs?: number | undefined;
  readonly requestTimeoutMs?: number | undefined;
  readonly signal?: AbortSignal | undefined;
}

export class Filesystem extends NativeFilesystem {
  /**
   * `watchDir(path, onEvent, opts)` de E2B; con un objeto como segundo
   * argumento se comporta como el nativo (`onEvent` dentro de las opciones).
   * Un `onEvent` asíncrono que rechaza se registra como aviso y el watch
   * sigue, igual que uno síncrono que lanza.
   */
  override watchDir(
    path: string,
    onEvent?: WatchEventCallback | WatchOptions,
    opts: WatchOpts = {},
  ): Promise<WatchHandle> {
    if (typeof onEvent !== "function") {
      return super.watchDir(path, onEvent);
    }
    return super.watchDir(path, {
      onEvent: settleAsyncCallback(onEvent, (error) =>
        this.core.logger?.warn?.("onEvent rechazó su promesa; el watch sigue", {
          reason: errorMessage(error),
        }),
      ),
      onExit: opts.onExit,
      recursive: opts.recursive,
      includeEntry: opts.includeEntry,
      user: opts.user,
      timeoutMs: opts.timeoutMs ?? DEFAULT_WATCH_TIMEOUT_MS,
      requestTimeoutMs: opts.requestTimeoutMs,
      signal: opts.signal,
    });
  }
}
