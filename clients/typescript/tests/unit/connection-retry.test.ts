/**
 * Un RPC cuya conexión con el proxy no llegó a abrirse (`Unavailable` con un
 * error de Node `syscall: "connect"`, visto en AWS justo tras `create()` y
 * `resume()`) se reintenta unas pocas veces: la petición nunca salió del
 * cliente. Cualquier otro error sale al primer intento. Espejo de
 * `test_connection_retry.py`.
 */

import { Code, ConnectError } from "@connectrpc/connect";
import { describe, expect, test } from "vitest";
import {
  CONNECT_RETRY_DELAYS_MS,
  isConnectionNotEstablished,
  retryUnestablished,
} from "../../src/transport/errors.js";

function refused(): ConnectError {
  const cause = Object.assign(new Error("connect ECONNREFUSED 127.0.0.1:443"), {
    code: "ECONNREFUSED",
    syscall: "connect",
  });
  return new ConnectError("connect ECONNREFUSED", Code.Unavailable, undefined, undefined, cause);
}

const PROXY_502 = new ConnectError("HTTP 502", Code.Unavailable);

function flaky<T>(failures: unknown[], answer: T) {
  const calls: number[] = [];
  const call = async (): Promise<T> => {
    calls.push(1);
    if (failures.length > 0) {
      throw failures.shift();
    }
    return answer;
  };
  return { call, calls };
}

describe("connection retry", () => {
  test("the budget outlasts a handshake closed for several seconds", () => {
    expect(CONNECT_RETRY_DELAYS_MS.reduce((total, ms) => total + ms, 0)).toBeGreaterThan(5000);
    expect(CONNECT_RETRY_DELAYS_MS.length).toBeGreaterThanOrEqual(5);
  });

  test("five closed handshakes in a row still succeed", async () => {
    const { call, calls } = flaky(
      Array.from({ length: 5 }, () => refused()),
      "ok",
    );
    expect(await retryUnestablished(call, async () => undefined)).toBe("ok");
    expect(calls).toHaveLength(6);
  });

  test("only a connection that never opened counts", () => {
    expect(isConnectionNotEstablished(refused())).toBe(true);
    expect(isConnectionNotEstablished(PROXY_502)).toBe(false);
    const reset = Object.assign(new Error("read ECONNRESET"), {
      code: "ECONNRESET",
      syscall: "read",
    });
    expect(
      isConnectionNotEstablished(
        new ConnectError("reset", Code.Unavailable, undefined, undefined, reset),
      ),
    ).toBe(false);
    expect(
      isConnectionNotEstablished(
        new ConnectError("x", Code.DeadlineExceeded, undefined, undefined, refused().cause),
      ),
    ).toBe(false);
  });

  test("a connection that never opened is retried with the bounded delays", async () => {
    const { call, calls } = flaky([refused(), refused()], "ok");
    const slept: number[] = [];
    expect(await retryUnestablished(call, async (ms) => void slept.push(ms))).toBe("ok");
    expect(calls).toHaveLength(3);
    expect(slept).toEqual(CONNECT_RETRY_DELAYS_MS.slice(0, 2));
  });

  test("the retries are bounded", async () => {
    const { call, calls } = flaky(
      Array.from({ length: 10 }, () => refused()),
      "ok",
    );
    await expect(retryUnestablished(call, async () => undefined)).rejects.toBeInstanceOf(
      ConnectError,
    );
    expect(calls).toHaveLength(CONNECT_RETRY_DELAYS_MS.length + 1);
  });

  test("any other error is not retried", async () => {
    const { call, calls } = flaky([PROXY_502], "ok");
    await expect(retryUnestablished(call, async () => undefined)).rejects.toBe(PROXY_502);
    expect(calls).toHaveLength(1);
  });
});
