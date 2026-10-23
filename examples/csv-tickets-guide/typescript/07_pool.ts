/**
 * Pool de sandboxes: preparación, toma y primera celda medidas por separado.
 * `start()` lanza el relleno en segundo plano; un `take()` sin plaza lista hace un `create()`
 * normal. Por eso se espera, con un límite, a que haya una plaza lista antes de medir la toma.
 * `await using` cierra el pool al salir (también si algo lanza) y termina las plazas aparcadas;
 * la plaza tomada es un sandbox normal y se termina aunque falle la celda.
 */
import { Sandbox, SandboxPool } from "rayito";
import { METADATA, runCell, TEMPLATE } from "./common.ts";

const FILL_LIMIT_MS = Number(process.env.POOL_FILL_LIMIT_MS ?? "120000");
const POLL_INTERVAL_MS = 500;
const SANDBOX_TIMEOUT_MS = 300_000;
const FIRST_CELL_TIMEOUT_MS = 30_000;
/** Vida máxima de cada plaza, también suspendida. */
const SLOT_LIFETIME_MS = 3_600_000;
/** Por debajo de esto la plaza se recicla. */
const SLOT_MIN_REMAINING_MS = 600_000;

const seconds = (ms: number): string => (ms / 1000).toFixed(2);
const sleep = (ms: number): Promise<void> => new Promise((resolve) => setTimeout(resolve, ms));

/** Mide `create()` y la primera celda de un sandbox nuevo. */
async function measureCreate(): Promise<void> {
  const t0 = performance.now();
  await using sbx = await Sandbox.create({ template: TEMPLATE, timeoutMs: SANDBOX_TIMEOUT_MS, metadata: METADATA });
  const t1 = performance.now();
  await runCell(sbx, "1 + 1", "primera celda", { timeoutMs: FIRST_CELL_TIMEOUT_MS });
  console.log(`create(): ${seconds(t1 - t0)} s | primera celda: ${seconds(performance.now() - t1)} s`);
}

/** Espera explícita y acotada a que el pool tenga una plaza lista. */
async function waitUntilReady(pool: SandboxPool, started: number): Promise<void> {
  while (pool.stats().ready < 1) {
    if (performance.now() - started > FILL_LIMIT_MS) {
      throw new Error("el pool no tuvo plaza lista a tiempo; no mido la toma");
    }
    await sleep(POLL_INTERVAL_MS);
  }
}

/** Mide la toma de una plaza preparada y su primera celda. */
async function measurePool(): Promise<void> {
  const t0 = performance.now();
  await using pool = await new SandboxPool({
    size: 1,
    template: TEMPLATE,
    timeoutMs: SLOT_LIFETIME_MS,
    minRemainingMs: SLOT_MIN_REMAINING_MS,
    metadata: METADATA,
  }).start();
  await waitUntilReady(pool, t0);
  const prepared = performance.now() - t0;
  const before = pool.stats();

  const t1 = performance.now();
  const sbx = await pool.take();
  const t2 = performance.now();
  let t3 = t2;
  try {
    await runCell(sbx, "1 + 1", "primera celda", { timeoutMs: FIRST_CELL_TIMEOUT_MS });
    t3 = performance.now();
  } finally {
    await sbx.kill();
  }
  const after = pool.stats();
  console.log(`preparación del pool: ${seconds(prepared)} s (fuera del cronómetro de la toma)`);
  console.log(`take(): ${seconds(t2 - t1)} s | primera celda: ${seconds(t3 - t2)} s`);
  console.log(after.hits - before.hits === 1 ? "plaza preparada (hit)" : "fallback a create() (miss)");
  console.log(`hits=${after.hits} misses=${after.misses} launched=${after.launched}`);
}

await measureCreate();
await measurePool();
