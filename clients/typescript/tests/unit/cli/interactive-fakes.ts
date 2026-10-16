/** Una terminal local y una PTY remota falsas: lo que usan los tests de `connect`, `create` y `runTerminal`. */

import { EventEmitter } from "node:events";
import type { InteractiveIo, TerminalInput } from "../../../src/cli/interactive-terminal.js";
import type {
  RemotePtyHandle,
  RemotePtyOptions,
  RemoteSandbox,
} from "../../../src/cli/services.js";
import type { PtySize } from "../../../src/models.js";

/** La entrada local: `emit("data", ...)` simula teclas y `emit("end")` el EOF de una tubería. */
export class FakeInput extends EventEmitter implements TerminalInput {
  readonly rawModes: boolean[] = [];
  paused = true;
  readonly isTTY: boolean | undefined;
  readonly setRawMode: ((mode: boolean) => void) | undefined;

  constructor(isTTY = false) {
    super();
    this.isTTY = isTTY ? true : undefined;
    this.setRawMode = isTTY ? (mode) => this.rawModes.push(mode) : undefined;
  }

  pause(): void {
    this.paused = true;
  }

  resume(): void {
    this.paused = false;
  }
}

export class FakeOutput extends EventEmitter {
  readonly chunks: Uint8Array[] = [];
  columns: number | undefined;
  rows: number | undefined;

  write(data: string | Uint8Array): boolean {
    this.chunks.push(typeof data === "string" ? Buffer.from(data) : data);
    return true;
  }

  get text(): string {
    return Buffer.concat(this.chunks).toString("utf8");
  }
}

export function fakeIo(
  input: FakeInput,
  output = new FakeOutput(),
): InteractiveIo & {
  readonly output: FakeOutput;
} {
  return { input, output };
}

/** El handle de una PTY: `finish` o `fail` deciden cómo acaba `wait()`. */
export class FakePtyHandle implements RemotePtyHandle {
  readonly inputs: Uint8Array[] = [];
  readonly resizes: PtySize[] = [];
  killed = 0;
  sendDelayMs = 0;
  sendFailure: Error | undefined;
  readonly #done: Promise<{ exitCode: number }>;
  #resolve!: (value: { exitCode: number }) => void;
  #reject!: (error: Error) => void;

  constructor() {
    this.#done = new Promise((resolve, reject) => {
      this.#resolve = resolve;
      this.#reject = reject;
    });
  }

  async sendInput(data: Uint8Array): Promise<void> {
    if (this.sendDelayMs > 0) {
      await new Promise((resolve) => setTimeout(resolve, this.sendDelayMs));
    }
    if (this.sendFailure !== undefined) {
      throw this.sendFailure;
    }
    this.inputs.push(data);
  }

  async resize(size: PtySize): Promise<void> {
    this.resizes.push(size);
  }

  async kill(): Promise<boolean> {
    this.killed += 1;
    this.finish(137);
    return true;
  }

  wait(): Promise<{ exitCode: number }> {
    return this.#done;
  }

  finish(exitCode: number): void {
    this.#resolve({ exitCode });
  }

  fail(error: Error): void {
    this.#reject(error);
  }

  get inputText(): string {
    return Buffer.concat(this.inputs).toString("utf8");
  }
}

export class FakePty {
  readonly handle = new FakePtyHandle();
  readonly created: RemotePtyOptions[] = [];
  /** Se llama justo al crear la PTY, para que el test decida cómo acaba. */
  onCreate: (handle: FakePtyHandle, options: RemotePtyOptions) => void = () => undefined;

  async create(options: RemotePtyOptions): Promise<RemotePtyHandle> {
    this.created.push(options);
    this.onCreate(this.handle, options);
    return this.handle;
  }
}

/** Un `RemoteSandbox` mínimo con la PTY falsa, para los tests que no usan nada más. */
export function sandboxWithPty(pty: FakePty): Pick<RemoteSandbox, "pty"> {
  return { pty };
}
