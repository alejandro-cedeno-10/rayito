/**
 * Lectura de "fallo diferido" por logs (investigación TPL-1/Q83, TPL-5/Q85).
 * Espejo de `rayito._templates._logs`: puro, recibe líneas de texto ya
 * traídas por `build.ts` (`GetLogEvents`), nunca llama a AWS.
 */

const STEP_PATTERN = /^#\d+ \[(\d+)\/(\d+)\] (.+)$/;
const EXIT_CODE_PATTERN = /exit code:\s*(\d+)/;
const LOG_TAIL_LINES = 20;
const CLIENT_ERROR_MARKER = "HTTP 4xx";
const SERVER_ERROR_MARKER = "HTTP 5xx";

export interface FailureDetail {
  readonly step: number | undefined;
  readonly command: string | undefined;
  readonly exitCode: number | undefined;
  readonly logTail: string | undefined;
}

/** El último paso `RUN` mencionado y el último `exit code:` visto en
 * `logLines` (ya en orden cronológico). */
export function parseBuildFailure(logLines: readonly string[]): FailureDetail {
  let step: number | undefined;
  let command: string | undefined;
  let exitCode: number | undefined;
  for (const line of logLines) {
    const stepMatch = STEP_PATTERN.exec(line.trim());
    if (stepMatch) {
      step = Number(stepMatch[1]);
      command = stepMatch[3];
    }
    const exitMatch = EXIT_CODE_PATTERN.exec(line);
    if (exitMatch?.[1] !== undefined) {
      exitCode = Number(exitMatch[1]);
    }
  }
  const tail = logLines.length > 0 ? logLines.slice(-LOG_TAIL_LINES).join("\n") : undefined;
  return { step, command, exitCode, logTail: tail };
}

/** `stateReason` de un `ready_cmd`/CMD que respondió 4xx o 5xx (Q85) ->
 * `"ready_client_error"`/`"ready_server_error"`; cualquier otro motivo da
 * `undefined`. */
export function classifyReadyFailure(stateReason: string | undefined): string | undefined {
  if (!stateReason) {
    return undefined;
  }
  if (stateReason.includes(SERVER_ERROR_MARKER)) {
    return "ready_server_error";
  }
  if (stateReason.includes(CLIENT_ERROR_MARKER)) {
    return "ready_client_error";
  }
  return undefined;
}
