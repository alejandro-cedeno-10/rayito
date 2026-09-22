import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";
import { PortSpec } from "../../src/aws/control-plane.js";
import { AuthenticationError } from "../../src/errors.js";
import { TOKEN_REFRESH_AFTER_MINUTES, TOKEN_REFRESH_RETRY_SECONDS } from "../../src/limits.js";
import { PROXY_AUTH_HEADER, proxyAuthInterceptor } from "../../src/transport/headers.js";
import {
  ProxyToken,
  TOKEN_REFRESH_AFTER_MS,
  TokenRefresher,
  TokenStore,
} from "../../src/transport/tokens.js";
import { RecordingLogger } from "./helpers.js";

const MINUTE = 60_000;
const PORT_8080 = [PortSpec.single(8080)];

describe("ProxyToken and TokenStore", () => {
  test("covers, refreshDue and msUntilRefresh", () => {
    const token = new ProxyToken("jwe", [PortSpec.single(8080), PortSpec.range(3000, 3010)], 0);
    expect(token.covers(8080)).toBe(true);
    expect(token.covers(3005)).toBe(true);
    expect(token.covers(9000)).toBe(false);
    expect(token.refreshDue(TOKEN_REFRESH_AFTER_MS - 1)).toBe(false);
    expect(token.refreshDue(TOKEN_REFRESH_AFTER_MS)).toBe(true);
    expect(token.msUntilRefresh(TOKEN_REFRESH_AFTER_MS - 10)).toBe(10);
    expect(token.msUntilRefresh(TOKEN_REFRESH_AFTER_MS + 10)).toBe(0);
    expect(TOKEN_REFRESH_AFTER_MS).toBe(TOKEN_REFRESH_AFTER_MINUTES * MINUTE);
  });

  test("the store indexes by port set and replaces same-set tokens", () => {
    const store = new TokenStore();
    store.put(new ProxyToken("a", PORT_8080, 0));
    store.put(new ProxyToken("b", [PortSpec.single(3000)], 0));
    store.put(new ProxyToken("c", PORT_8080, 1));
    expect(store.tokens().map((token) => token.jwe)).toEqual(["b", "c"]);
    expect(store.jweFor(8080)).toBe("c");
    expect(store.jweFor(3000)).toBe("b");
    expect(store.jweFor(4000)).toBeUndefined();
    store.clear();
    expect(store.tokens()).toEqual([]);
  });
});

describe("proxyAuthInterceptor", () => {
  test("throws AuthenticationError before sending when the store has no token", async () => {
    const interceptor = proxyAuthInterceptor(new TokenStore(), { port: 3000, accessToken: "t" });
    const next = vi.fn();
    const request = { header: new Headers() } as unknown as Parameters<
      ReturnType<typeof interceptor>
    >[0];
    await expect(interceptor(next)(request)).rejects.toThrow(AuthenticationError);
    await expect(interceptor(next)(request)).rejects.toThrow(/puerto 3000/);
    expect(next).not.toHaveBeenCalled();
  });

  test("sets the four lowercase headers per call and reads the store each time", async () => {
    const store = new TokenStore();
    store.put(new ProxyToken("first", PORT_8080, 0));
    const interceptor = proxyAuthInterceptor(store, { port: 8080, accessToken: "tok" });
    const next = vi.fn(async (request: { header: Headers }) => request);
    const call = interceptor(next as never);
    const first = { header: new Headers() };
    await call(first as never);
    expect(Object.fromEntries(first.header.entries())).toEqual({
      "x-aws-proxy-auth": "first",
      "x-aws-proxy-port": "8080",
      "x-aws-proxy-force-h2": "true",
      "x-access-token": "tok",
    });
    store.put(new ProxyToken("second", PORT_8080, 1));
    const second = { header: new Headers() };
    await call(second as never);
    expect(second.header.get(PROXY_AUTH_HEADER)).toBe("second");
  });

  test("omits x-access-token when no access token is given", async () => {
    const store = new TokenStore();
    store.put(new ProxyToken("jwe", PORT_8080, 0));
    const interceptor = proxyAuthInterceptor(store, { port: 8080, accessToken: undefined });
    const request = { header: new Headers() };
    await interceptor((async (r: unknown) => r) as never)(request as never);
    expect(request.header.has("x-access-token")).toBe(false);
  });
});

describe("TokenRefresher", () => {
  beforeEach(() => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2026-09-16T12:00:00Z"));
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  function refresher(
    mint: (ports: readonly PortSpec[]) => Promise<string>,
    logger?: RecordingLogger,
  ) {
    return new TokenRefresher(new TokenStore(), mint, { now: Date.now, logger });
  }

  test("mint stores the token and ensure reuses a covering one", async () => {
    const mint = vi.fn(
      async (ports: readonly PortSpec[]) => `jwe-${ports.map((p) => p.start).join(",")}`,
    );
    const refresh = refresher(mint);
    const token = await refresh.mint([PortSpec.single(8080), PortSpec.single(3000)]);
    expect(token.jwe).toBe("jwe-8080,3000");
    expect(await refresh.ensure(3000)).toBe(token);
    expect(mint).toHaveBeenCalledTimes(1);
    const other = await refresh.ensure(4000);
    expect(other.ports).toEqual([PortSpec.single(4000)]);
    expect(mint).toHaveBeenCalledTimes(2);
    expect(refresh.store.jweFor(4000)).toBe("jwe-4000");
  });

  test("refresh schedule: nothing at 44 min, one mint per port set by 46 min", async () => {
    const mint = vi.fn(async () => `jwe-${mint.mock.calls.length}`);
    const refresh = refresher(mint);
    await refresh.mint(PORT_8080);
    await refresh.mint([PortSpec.single(3000)]);
    refresh.start();
    expect(refresh.scheduled).toBe(true);
    await vi.advanceTimersByTimeAsync(44 * MINUTE);
    expect(mint).toHaveBeenCalledTimes(2);
    await vi.advanceTimersByTimeAsync(2 * MINUTE);
    expect(mint).toHaveBeenCalledTimes(4);
    expect(refresh.store.jweFor(8080)).toBe("jwe-3");
    expect(refresh.store.jweFor(3000)).toBe("jwe-4");
    expect(refresh.scheduled).toBe(true);
    refresh.stop();
    expect(refresh.scheduled).toBe(false);
    await vi.advanceTimersByTimeAsync(60 * MINUTE);
    expect(mint).toHaveBeenCalledTimes(4);
  });

  test("retries 60 s after a failed mint and logs a warning without the JWE", async () => {
    let fail = false;
    const mint = vi.fn(async () => {
      if (fail) {
        throw new Error("ThrottlingException: slow down");
      }
      return "eyJ-secret-jwe";
    });
    const logger = new RecordingLogger();
    const refresh = refresher(mint, logger);
    await refresh.mint(PORT_8080);
    refresh.start();
    fail = true;
    await vi.advanceTimersByTimeAsync(45 * MINUTE);
    expect(mint).toHaveBeenCalledTimes(2);
    expect(refresh.store.jweFor(8080)).toBe("eyJ-secret-jwe");
    fail = false;
    await vi.advanceTimersByTimeAsync(TOKEN_REFRESH_RETRY_SECONDS * 1000);
    expect(mint).toHaveBeenCalledTimes(3);
    expect(logger.at("warn")).toHaveLength(1);
    expect(logger.dump()).not.toContain("eyJ-secret-jwe");
    refresh.stop();
  });

  test("refreshAll re-mints every stored token and refreshDue is deterministic", async () => {
    const mint = vi.fn(async () => `jwe-${mint.mock.calls.length}`);
    const refresh = refresher(mint);
    await refresh.mint(PORT_8080);
    await refresh.mint([PortSpec.single(3000)]);
    await refresh.refreshAll();
    expect(mint).toHaveBeenCalledTimes(4);
    expect(await refresh.refreshDue(Date.now())).toBe(true);
    expect(mint).toHaveBeenCalledTimes(4);
    expect(await refresh.refreshDue(Date.now() + TOKEN_REFRESH_AFTER_MS)).toBe(true);
    expect(mint).toHaveBeenCalledTimes(6);
    expect(refresh.msUntilNextRefresh(Date.now())).toBe(TOKEN_REFRESH_AFTER_MS);
    expect(new TokenRefresher(new TokenStore(), mint).msUntilNextRefresh()).toBe(
      TOKEN_REFRESH_AFTER_MS,
    );
  });

  test("start is idempotent, stop is idempotent and the timer is unref'd", async () => {
    const mint = vi.fn(async () => "jwe");
    const refresh = refresher(mint);
    await refresh.mint(PORT_8080);
    const spy = vi.spyOn(globalThis, "setTimeout");
    refresh.start();
    refresh.start();
    const timer = spy.mock.results[spy.mock.results.length - 1]?.value as {
      hasRef?: () => boolean;
    };
    expect(timer.hasRef?.()).toBe(false);
    refresh.stop();
    refresh.stop();
    expect(refresh.scheduled).toBe(false);
    spy.mockRestore();
  });

  test("concurrent mints of the same port set share one control-plane call", async () => {
    const mint = vi.fn(async () => "jwe");
    const refresh = refresher(mint);
    await Promise.all([refresh.mint(PORT_8080), refresh.mint(PORT_8080)]);
    expect(mint).toHaveBeenCalledTimes(1);
  });
});
