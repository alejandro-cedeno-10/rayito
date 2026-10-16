/**
 * Abre una URL en el navegador del usuario (`rayito dashboard --open`), como
 * `webbrowser.open` de Python: si no hay navegador, no pasa nada. Es de las
 * pocas piezas que lanzan un proceso, y sólo la usa el ejecutable.
 */

import { spawn } from "node:child_process";

const OPENERS: Readonly<Record<string, readonly [string, readonly string[]]>> = {
  darwin: ["open", []],
  win32: ["rundll32", ["url.dll,FileProtocolHandler"]],
};
const DEFAULT_OPENER: readonly [string, readonly string[]] = ["xdg-open", []];

export function openUrl(url: string): Promise<void> {
  const [command, leading] = OPENERS[process.platform] ?? DEFAULT_OPENER;
  return new Promise((resolve) => {
    const child = spawn(command, [...leading, url], { stdio: "ignore", detached: true });
    child.once("error", () => resolve());
    child.once("spawn", () => {
      child.unref();
      resolve();
    });
  });
}
