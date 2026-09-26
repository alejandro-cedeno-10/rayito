# rayito (SDK TypeScript)

Cliente TypeScript de Rayito: sandboxes de ejecución para agentes de IA sobre
AWS Lambda MicroVMs, dentro de tu propia cuenta. La misma superficie que el
SDK Python (`clients/python`) en camelCase y milisegundos, generada desde el
mismo `.proto` y validando los mismos límites (`limits.json`).

```bash
pnpm add rayito        # o: npm i rayito / yarn add rayito / bun add rayito
```

El paquete trae ESM y CommonJS con sus tipos, y el shim de E2B en
`rayito/e2b`.

```ts
import { Sandbox } from "rayito";

await using sbx = await Sandbox.create({ template: "rayito-base-2gb", timeoutMs: 3_600_000 });
console.log(await sbx.isRunning());
```

Requisitos: Node >= 20, credenciales de AWS en la cadena por defecto del SDK
v3 (`AWS_PROFILE`/`AWS_REGION` o variables de entorno) y una imagen
`rayito-base` publicada desde este árbol en la cuenta (las novedades de 0.3.0
exigen el `rayd` de M9; `rayito doctor` lo comprueba).

Estado: M6. `Sandbox.create/connect/kill/list/getInfo/isRunning/getHost/
pause/resume/getHealth/getMetrics`, `sbx.commands` (`run` en foreground y
background, `list`, `kill`, `connect`, `sendStdin`, `closeStdin`;
`CommandHandle` con `wait/kill/disconnect/sendStdin/closeStdin` e iteración
`for await`), `sbx.files` (`read` en `text`/`bytes`/`stream`, `write`,
`writeFiles` en un solo stream, `list({ depth })`, `exists`, `getInfo`,
`remove`, `rename`, `makeDir`, `watchDir` con `WatchHandle`), `sbx.pty`
(`create`, `connect`, `sendInput`, `resize`, `kill`; `PtyHandle` es un
`CommandHandle` que entrega `{ pty: Uint8Array }`) y `sbx.runCode` (kernel
Jupyter con estado, `Execution` con `results`/`logs`/`error`, `Result` con
`text`/`html`/`png`/`chart`/`data`…, `createCodeContext`/`listCodeContexts`/
`removeCodeContext`/`restartCodeContext`) funcionan contra el plano de control
de AWS (`@aws-sdk/client-lambda-microvms`) y el agente (`@connectrpc/connect-node`
sobre HTTP/2 a través del proxy de AWS). Todo es asíncrono: no hay árbol
síncrono (Node no tiene cliente gRPC bloqueante, como en el SDK JS de E2B).

Suspend/resume: `sbx.pause()` congela el MicroVM (procesos, PTY, watches y
variables del kernel incluidos) y `sbx.resume()` lo reanuda. Los handles,
watches y `runCode` que estaban abiertos se reenganchan solos la próxima vez
que se leen (`Connect(pid, fromSeq)`, `Pty.Connect`, `WatchDir` de nuevo,
`Reattach`), los unarios cortados se reintentan una vez y los timeouts del
servidor excluyen el tiempo suspendido. `reconnectTimeoutMs` (60 000) acota
cuánto espera el SDK al agente una vez que el VM vuelve a `RUNNING`; mientras
está suspendido, **leer un handle, una PTY o un watch no lo despierta** ni
consume ese presupuesto: la lectura se bloquea hasta `resume()`, hasta que
otra llamada lo reanude (con auto-resume, cualquier llamada nueva como
`commands.run` o `files.write` sí lo despierta) o hasta que el VM termine. Un
`run`/`runCode` en foreground que un `pause()` de ese mismo `Sandbox` corta a
mitad también espera al `resume()` y continúa donde estaba (`Reattach`); si
lo suspendió el idle, el propio `run` lo despierta. `getHealth()` expone
`resumeGeneration`, `clockOffsetMs` y `kernelStateLost`.

```ts
import { Sandbox } from "rayito";

await using sbx = await Sandbox.create();
console.log((await sbx.commands.run("echo hola")).stdout);
const server = await sbx.commands.run("python3 -m http.server 3000", {
  background: true,
  timeoutMs: 0,
});
const host = await sbx.getHost(3000);
console.log(`https://${host}`, host.headers);
await server.kill();

const info = await sbx.files.write("/home/user/data.csv", "a,b\n1,2\n");
console.log(info.size, await sbx.files.read("data.csv"));
{
  await using watch = await sbx.files.watchDir("/home/user");
  await sbx.commands.run("touch /home/user/new.txt");
  console.log(watch.getNewEvents());
}

await sbx.runCode("x = 42");
console.log((await sbx.runCode("x")).text); // "42"
const plot = await sbx.runCode("import matplotlib.pyplot as plt; plt.plot([1, 2, 3]); plt.show()");
console.log(plot.results[0]?.formats()); // ["png", "chart"]
const failed = await sbx.runCode("1/0");
console.log(failed.error?.name); // "ZeroDivisionError", nunca una excepción
const ctx = await sbx.createCodeContext({ cwd: "/tmp" });
console.log((await sbx.runCode("x", { context: ctx })).error?.name); // "NameError": otro kernel
// Kernel bash (variante de imagen rayito-base-poly): arranca en la primera celda
console.log((await sbx.runCode("echo hi", { language: "bash" })).logs.stdout.join("")); // "hi\n"

const pty = await sbx.pty.create({
  size: { cols: 120, rows: 40 },
  onData: (chunk) => process.stdout.write(chunk),
  timeoutMs: 0,
});
await pty.sendInput("echo hola\n");
for await (const { pty: data } of pty) {
  // { pty: Uint8Array } por chunk de la terminal
  if (data !== undefined && new TextDecoder().decode(data).includes("hola\r\n")) {
    break;
  }
}
await pty.resize({ cols: 80, rows: 24 });
await pty.kill();

await sbx.pause(); // SUSPENDED; todo sigue vivo al otro lado
await sbx.resume();
console.log((await sbx.runCode("x")).text); // "42": el kernel conservó su estado
console.log((await sbx.getHealth()).resumeGeneration); // 1
```

`await using` requiere TypeScript >= 5.2 con `lib: ["ESNext.Disposable"]` (o
un target que lo emita); en Node 20.0–20.3 el paquete instala el polyfill de
`Symbol.asyncDispose`. Sin `await using`, `try { … } finally { await sbx.kill() }`
hace lo mismo; `sbx.close()` cierra las dos sesiones HTTP/2 (unarios y
streams) sin tocar el VM.

Errores: la misma jerarquía que el SDK Python con los nombres del SDK JS de
E2B: `SandboxError` (raíz) con `TimeoutError`, `InvalidArgumentError`,
`NotFoundError` (`FileNotFoundError`, `SandboxNotFoundError`),
`SandboxNotReadyError`, `SandboxStateError`, `SandboxLifetimeError`,
`CommandExitError` (`exitCode`, `stdout`, `stderr`) y `RateLimitError`; fuera
de la jerarquía `AuthenticationError` (`proxyRejected`), `QuotaExceededError`
y `CapacityError`. `instanceof` funciona en ESM y en CommonJS.

Opciones de `Sandbox.create`: `template` (o `RAYITO_TEMPLATE`), `templateVersion`,
`timeoutMs` (3 600 000; máximo 28 800 000), `idle` (`{ maxIdleSeconds: 300,
autoResume: true }` por defecto; `null` desactiva el auto-suspend), `envs`,
`executionRoleArn`, `allowedPorts` (8080 siempre; 9000 prohibido), `ingress`/
`egress` (conectores gestionados o ARNs), `logging` (`"disabled"`,
`"cloudwatch"` o el objeto de la API), `region`, `accessToken` (o
`RAYITO_ACCESS_TOKEN`), `readyTimeoutMs` (90 000), `requestTimeoutMs`
(60 000), `reconnectTimeoutMs` (60 000), `keepOnFailure`, `controlPlane`,
`client` (un `LambdaMicrovmsClient` propio), `transport` y `logger`. Un
`logger` opcional recibe ids de sandbox, estados, generaciones y duraciones;
nunca salida, ficheros, código, tokens ni cabeceras.

## Novedades de 0.3.0 (M9)

Paridad con E2B 2.x. Exige una imagen publicada con el `rayd` de M9 y está
aceptada contra AWS real (2026-09-24). Qué imagen necesita cada cosa:
`docs/site/docs/images.md`.

```ts
import { ALL_TRAFFIC, Sandbox } from "rayito";

// Plazo del servidor (docs/site/docs/lifecycle.md, ADR-011)
await using sbx = await Sandbox.create({
  timeoutMs: 600_000,             // plazo lógico que impone rayd
  maxLifetimeMs: 7_200_000,       // tope de la plataforma, running + suspendido (≤ 8 h)
  onTimeout: "kill",              // o "pause" (necesita idle)
  transfer: { bucket: "amzn-s3-demo-bucket" },  // URLs de S3 y ficheros grandes (ADR-010)
});
await sbx.setTimeout(1_800_000);  // SetTimeout EXACT: alarga o acorta, hasta maxLifetimeMs
await sbx.connect({ timeoutMs: 900_000 });     // reanuda si hace falta y sólo alarga

// Transferencias por S3 (docs/site/docs/files.md)
const ticket = await sbx.files.uploadUrl("/home/user/in.csv", { expiresIn: 900 });
await fetch(ticket.url, { method: "PUT", body: "a,b\n1,2\n", headers: ticket.headers });
await ticket.wait();
const link = await sbx.files.downloadUrl("/home/user/in.csv");
await sbx.files.write("/home/user/log.txt", "texto", { gzip: true, metadata: { origen: "ci" } });

// Métricas y listado (docs/site/docs/observability.md)
const history = await sbx.getMetricsHistory({ start: new Date(Date.now() - 600_000) });
const pages = Sandbox.paginate({ limit: 20, order: "desc" });
const first = await pages.nextItems();

// Git (docs/site/docs/git.md)
await sbx.git.clone("https://github.com/octo/demo.git", { path: "/home/user/demo", depth: 1 });
console.log((await sbx.git.status("/home/user/demo")).currentBranch, link.size, history.length, first.length);

// JavaScript y TypeScript con Deno, sólo rayito-base-poly (docs/site/docs/kernels.md)
await using poly = await Sandbox.create({ template: "rayito-base-poly" });
console.log((await poly.runCode("const x: number = 40 + 2; x", { language: "typescript" })).text);

// Red saliente de E2B, sólo rayito-base-caps (docs/site/docs/network.md)
await using caps = await Sandbox.create({
  template: "rayito-base-caps",
  network: { denyOut: [ALL_TRAFFIC], allowOut: ["api.example.com"] },
});
await caps.updateNetwork({ denyOut: [ALL_TRAFFIC] });
```

- `onTimeout: "kill"` hace que `rayd` salga al vencer y la VM termine sin IAM
  (≈ 15 s después); `"pause"` la suspende. Contra una imagen anterior a M9,
  pedir un ciclo de vida es `LifecycleUnsupportedError` y el VM se termina.
- `network: { allowOut, denyOut, egressProxy }` y `allowInternetAccess: false`
  (con `ALL_TRAFFIC`) aplican la política de E2B dentro del guest de
  `rayito-base-caps`; en otra imagen el SDK termina el VM y lanza
  `UnimplementedError`. `updateNetwork()` y `getNetwork()` la cambian y la leen.
- `signal` (`AbortSignal`) cancela `create`, `connect`, `setTimeout`,
  `getMetricsHistory`, `updateNetwork` y el resto de llamadas de ciclo de vida.
- Un disco lleno es ahora `DiskFullError` (antes `RateLimitError`).

### Shim de E2B: `rayito/e2b`

```ts
// antes: import { Sandbox } from "@e2b/code-interpreter";
import { Sandbox } from "rayito/e2b";

await using sbx = await Sandbox.create({ timeoutMs: 300_000, metadata: { run: "42" } });
console.log((await sbx.runCode("1 + 1")).text);
await sbx.setTimeout(600_000);
await Sandbox.setTimeout(sbx.sandboxId, 900_000, { accessToken: sbx.native.accessToken });
```

`rayito/e2b` es el shim de la API JS de E2B 2.x dentro del mismo paquete:
`Sandbox.create`, estáticos `kill`/`getInfo`/`getFullInfo`/`isRunning`/
`connect`/`pause`/`betaPause`/`setTimeout`/`getMetrics`/`list`/`updateNetwork`,
`uploadUrl`/`downloadUrl` (con `RAYITO_TRANSFER_BUCKET`), `git`, `getHost`
síncrono, `ConnectionConfig`, `E2B`; lo que Lambda MicroVMs no puede hacer
lanza `UnimplementedError` (tablas en `docs/site/docs/e2b-compat.md` y
`docs/site/docs/e2b-parity.md`).

## Desarrollo

```bash
cd clients/typescript
pnpm install --frozen-lockfile
pnpm lint        # biome check .
pnpm typecheck   # tsc --noEmit
pnpm build       # tsdown: dist/index.{mjs,cjs,d.mts,d.cts}
pnpm test        # vitest, proyecto unit (rayd falso en loopback + plano de control falso)
pnpm pack:check  # empaqueta y lista el tarball (README, LICENSE, dist/*)
RAYITO_E2E=1 RAYITO_TEMPLATE=<arn-o-nombre> pnpm test:e2e   # AWS real (tests/e2e/m6.e2e.test.ts)
```

El código generado vive en `src/gen/rayito/v1/*_pb.ts` y se regenera desde la
raíz del repo con `make proto` (`buf generate`, plugin remoto
`buf.build/bufbuild/es:v2.15.0`). Si el BSR no es accesible desde tu máquina:
`pnpm add -D @bufbuild/protoc-gen-es@2.15.0` y un `buf.gen.local.yaml` con
`plugins: [{ local: ["pnpm", "--dir", "clients/typescript", "exec",
"protoc-gen-es"], out: clients/typescript/src/gen, opt: [target=ts,
import_extension=js] }]`; `buf generate --template buf.gen.local.yaml`
produce exactamente los mismos ficheros (misma versión del plugin). Los
límites de la API (`src/limits.ts`) se generan con `make limits` desde
`limits.json`; `tests/unit/limits.test.ts` falla si alguien los edita a mano.

Variables del e2e: `RAYITO_E2E=1` y `RAYITO_TEMPLATE` (obligatorias),
`AWS_PROFILE`/`AWS_REGION` (cadena por defecto del SDK) y
`RAYITO_EXECUTION_ROLE_ARN` (activa `logging: "cloudwatch"`). Cada sandbox del
e2e cuesta ≈ $0.03; la suite usa dos y termina todo lo que crea.
