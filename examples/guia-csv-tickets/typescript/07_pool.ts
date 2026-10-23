// Pool de sandboxes: preparación, toma y primera celda medidas por separado.
// start() lanza el relleno en segundo plano; un take() sin plaza lista hace un create() normal.
import { Sandbox, SandboxPool } from "rayito";
import { METADATA, TEMPLATE, runCell } from "./comun.ts";

const FILL_LIMIT_MS = Number(process.env.POOL_FILL_LIMIT_S ?? "120") * 1000;
const seg = (ms: number): string => (ms / 1000).toFixed(2);

{
  const t0 = performance.now();
  await using sbx = await Sandbox.create({ template: TEMPLATE, timeoutMs: 300_000, metadata: METADATA });
  const t1 = performance.now();
  await runCell(sbx, "1 + 1", "primera celda", 30_000);
  console.log(`create(): ${seg(t1 - t0)} s | primera celda: ${seg(performance.now() - t1)} s`);
}

const t0 = performance.now();
// await using cierra el pool al salir (también si algo lanza) y termina las plazas aparcadas
await using pool = await new SandboxPool({
  size: 1,
  template: TEMPLATE,
  timeoutMs: 3_600_000, // vida máxima de cada plaza, también suspendida
  minRemainingMs: 600_000, // por debajo de esto la plaza se recicla
  metadata: METADATA,
}).start();

while (pool.stats().ready < 1) {
  // espera explícita y acotada
  if (performance.now() - t0 > FILL_LIMIT_MS) throw new Error("el pool no tuvo plaza lista a tiempo; no mido la toma");
  await new Promise((r) => setTimeout(r, 500));
}
const preparado = performance.now() - t0;
const antes = pool.stats();

const t1 = performance.now();
const sbx = await pool.take();
const t2 = performance.now();
let t3 = t2;
try {
  await runCell(sbx, "1 + 1", "primera celda", 30_000);
  t3 = performance.now();
} finally {
  await sbx.kill();
}
const despues = pool.stats();
console.log(`preparación del pool: ${seg(preparado)} s (fuera del cronómetro de la toma)`);
console.log(`take(): ${seg(t2 - t1)} s | primera celda: ${seg(t3 - t2)} s`);
console.log(despues.hits - antes.hits === 1 ? "plaza preparada (hit)" : "fallback a create() (miss)");
console.log(`hits=${despues.hits} misses=${despues.misses} launched=${despues.launched}`);
