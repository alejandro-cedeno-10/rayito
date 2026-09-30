/**
 * `LifecycleService` como lo implementa `rayd` (ADR-011): `SetTimeout` exige
 * `x-access-token`, graba cada petición y mueve el `LifecycleState` que
 * `Health` devuelve (`FakeHealth.lifecycle`). Sin estado en `Health` responde
 * `Unimplemented`, como un agente anterior a M9; `UNMANAGED` responde
 * `FailedPrecondition "lifecycle_unmanaged"` y un plazo más allá del tope
 * `InvalidArgument "timeout beyond cap; cap_unix_ms=<n>"`. También las formas
 * de cierre `sandbox_timeout` de los streams.
 */

import { clone, create } from "@bufbuild/protobuf";
import { Code, ConnectError, type HandlerContext } from "@connectrpc/connect";
import { StreamErrorSchema } from "../../../src/gen/rayito/v1/common_pb.js";
import {
  LifecyclePhase,
  type LifecycleState,
  LifecycleStateSchema,
  type SetTimeoutRequest,
  TimeoutAction,
  TimeoutMode,
} from "../../../src/gen/rayito/v1/lifecycle_pb.js";
import { type EndEvent, EndEventSchema } from "../../../src/gen/rayito/v1/process_pb.js";
import { type PtyExited, PtyExitedSchema } from "../../../src/gen/rayito/v1/pty_pb.js";
import { assertProxyHeaders, type HeaderMap, headerMap, requireAccessToken } from "./common.js";
import type { FakeHealth } from "./health.js";

export interface SetTimeoutCall {
  readonly timeoutMs: number;
  readonly mode: TimeoutMode;
  readonly headers: HeaderMap;
}

export interface ManagedLifecycleOptions {
  readonly phase?: LifecyclePhase;
  readonly deadlineInMs?: number;
  readonly capInMs?: number;
  readonly timeoutMs?: number;
  readonly onTimeout?: "kill" | "pause";
  readonly autoResume?: boolean;
  readonly now?: number;
}

/** Un `LifecycleState` gestionado con instantes relativos a `now` (por defecto 90 s de plazo, 840 s de tope). */
export function managedLifecycle(options: ManagedLifecycleOptions = {}): LifecycleState {
  const now = options.now ?? Date.now();
  return create(LifecycleStateSchema, {
    phase: options.phase ?? LifecyclePhase.ACTIVE,
    deadlineUnixMs: BigInt(now + (options.deadlineInMs ?? 90_000)),
    capUnixMs: BigInt(now + (options.capInMs ?? 840_000)),
    timeoutMs: BigInt(options.timeoutMs ?? 60_000),
    onTimeout: options.onTimeout === "pause" ? TimeoutAction.PAUSE : TimeoutAction.KILL,
    autoResume: options.autoResume ?? false,
    extensions: 0,
  });
}

export function unmanagedLifecycle(): LifecycleState {
  return create(LifecycleStateSchema, { phase: LifecyclePhase.UNMANAGED });
}

/** `EndEvent` con el que `rayd` cierra `Start`/`Connect` al vencer el plazo. */
export function sandboxTimeoutEnd(): EndEvent {
  return create(EndEventSchema, {
    exitCode: 0,
    exited: false,
    status: "sandbox_timeout",
    error: create(StreamErrorSchema, { code: "sandbox_timeout", message: "sandbox timeout" }),
  });
}

/** `PtyExited` con el que `rayd` cierra `Create`/`Connect` de una PTY al vencer el plazo. */
export function ptySandboxTimeoutExited(): PtyExited {
  return create(PtyExitedSchema, {
    exitCode: 0,
    exited: false,
    status: "sandbox_timeout",
    error: create(StreamErrorSchema, { code: "sandbox_timeout", message: "sandbox timeout" }),
  });
}

export class FakeLifecycleService {
  readonly tokenSha256: string;
  readonly health: FakeHealth;
  readonly calls: SetTimeoutCall[] = [];
  readonly failNext: ConnectError[] = [];

  constructor(tokenSha256: string, health: FakeHealth) {
    this.tokenSha256 = tokenSha256;
    this.health = health;
  }

  setTimeout(request: SetTimeoutRequest, context: HandlerContext): LifecycleState {
    const headers = headerMap(context);
    assertProxyHeaders(headers);
    requireAccessToken(headers, this.tokenSha256);
    this.calls.push({ timeoutMs: Number(request.timeoutMs), mode: request.mode, headers });
    const scripted = this.failNext.shift();
    if (scripted !== undefined) {
      throw scripted;
    }
    const current = this.health.lifecycle;
    if (current === undefined) {
      throw new ConnectError("rayito.v1.LifecycleService is not implemented", Code.Unimplemented);
    }
    const next = moved(current, request);
    this.health.lifecycle = next;
    return clone(LifecycleStateSchema, next);
  }
}

function moved(current: LifecycleState, request: SetTimeoutRequest): LifecycleState {
  if (request.mode === TimeoutMode.UNSPECIFIED) {
    throw new ConnectError("mode must be EXACT or AT_LEAST", Code.InvalidArgument);
  }
  if (request.timeoutMs < 1000n) {
    throw new ConnectError("timeout below 1 s", Code.InvalidArgument);
  }
  if (current.phase === LifecyclePhase.UNMANAGED) {
    throw new ConnectError("lifecycle_unmanaged", Code.FailedPrecondition);
  }
  const target = BigInt(Date.now()) + request.timeoutMs;
  if (target > current.capUnixMs) {
    throw new ConnectError(
      `timeout beyond cap; cap_unix_ms=${current.capUnixMs}`,
      Code.InvalidArgument,
    );
  }
  const keepLonger =
    request.mode === TimeoutMode.AT_LEAST &&
    current.phase === LifecyclePhase.ACTIVE &&
    current.deadlineUnixMs > target;
  const deadline = keepLonger ? current.deadlineUnixMs : target;
  const next = clone(LifecycleStateSchema, current);
  next.phase = LifecyclePhase.ACTIVE;
  if (deadline !== current.deadlineUnixMs) {
    next.deadlineUnixMs = deadline;
    next.timeoutMs = request.timeoutMs;
    next.extensions += 1;
  }
  return next;
}
