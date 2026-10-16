/** Una terminal falsa: stdout y stderr en buffers y la entrada como líneas. */

import type { Terminal } from "../../../src/cli/console.js";
import type { TerminalInput } from "../../../src/cli/interactive-terminal.js";

export class FakeTerminal implements Terminal {
  /** La entrada cruda de las sesiones interactivas; sin ella `connect` y `create` no abren terminal. */
  stdin: TerminalInput | undefined;
  stdoutText = "";
  stderrText = "";
  readonly #lines: string[];

  readonly #echoAnswers: boolean;

  /** `echoAnswers` copia cada respuesta tras su pregunta, como `CliRunner` de click. */
  constructor(stdin = "", echoAnswers = true) {
    this.#lines = stdin === "" ? [] : stdin.replace(/\n$/, "").split("\n");
    this.#echoAnswers = echoAnswers;
  }

  readonly stdout = {
    write: (text: string | Uint8Array): boolean => {
      this.stdoutText += typeof text === "string" ? text : Buffer.from(text).toString("utf8");
      return true;
    },
  };

  readonly stderr = {
    write: (text: string | Uint8Array): boolean => {
      this.stderrText += typeof text === "string" ? text : Buffer.from(text).toString("utf8");
      return true;
    },
  };

  async readLine(prompt: string): Promise<string | undefined> {
    this.stdoutText += prompt;
    const answer = this.#lines.shift();
    if (answer !== undefined && this.#echoAnswers) {
      this.stdoutText += `${answer}\n`;
    }
    return answer;
  }
}
