## Why

`ci.yml`'s job `check` (x86_64, `ubuntu-24.04`) sólo corre
`cargo test --workspace --locked --no-fail-fast --lib --bins`
(3c671f4a): las suites de integración de `rayd` (`crates/rayd/tests/*.rs`, 17
binarios) congelan intermitentemente ese runner con la fuerza suficiente para
que ni el `timeout --kill-after` del paso (ebb7089) ni la cancelación del job
respondan — el runner queda mudo hasta que GitHub lo da por perdido. El job
`arm` (`ubuntu-24.04-arm`) sí corre la suite completa porque `rayd` sólo se
distribuye para `aarch64-unknown-linux-musl`, así que x86 nunca ha probado
esas suites desde 3c671f4a salvo en máquinas de desarrollador.

Antes de ese commit, dos intentos ya habían tocado la causa más obvia: 18e5b52
hizo que los procesos de usuario no hereden descriptores del runner más allá
de stdio (fd07d9b/565c49e), y ebb7089 volcó la salida a fichero con timeout
duro por si era un proceso huérfano reteniendo el paso — ninguno de los dos
evitó el cuelgue duro, así que la causa no está (solo) en la herencia de fds
ni en la falta de timeout.

## What Changes

- Añadir un workflow de sondeo, throwaway y nunca fusionado a `main`
  (`.github/workflows/ci-x86-probe.yml`, en una rama `ci/x86-freeze-probe`,
  PR en borrador), con un job por matriz — uno por binario de test de
  integración — que instrumenta cada ejecución (diagnóstico del runner,
  muestreo cada 10 s al log en vivo, `dmesg` al final) para capturar el
  cuelgue con evidencia en vez de un timeout mudo.
- Según lo que el sondeo confirme, exactamente una de estas acciones sobre
  `ci.yml`, job `check` únicamente (el job `arm` no cambia):
  1. si la causa está en el harness o en un fichero de test
     (`crates/rayd/tests/**`, propiedad de este cambio): arreglarla ahí y
     reactivar la suite completa de x86 con el mismo wrapper de timeout que
     ya usa `arm`;
  2. si la causa está en `rayd` src (fuera del alcance de este cambio):
     no tocar `src/`; dejar reportada la causa y la corrección propuesta para
     el orquestador;
  3. si sólo algunos binarios concretos congelan y la causa no se puede
     arreglar desde `tests/`: reactivar en x86 todos los binarios salvo esos,
     con lista explícita, documentando los excluidos;
  4. si el cuelgue no se reproduce en >= 5 ejecuciones completas del sondeo:
     reactivar la suite completa en x86 con el wrapper de timeout y dejar
     documentado el resultado negativo.
- Documentar el proceso completo (ejecuciones, evidencia, hipótesis
  descartadas, causa raíz o "no reproducido", decisión sobre `ci.yml`) en
  `docs/research/2026-10-ci-x86-freeze.md`.
- Borrar `ci-x86-probe.yml` y cerrar el PR en borrador antes de fusionar el
  cambio real: el workflow de sondeo no debe llegar a `main`.

## Impact

- Affected specs: ninguno (no cambia texto de requisito; `ci.yml` es
  infraestructura de CI, no comportamiento de producto).
- Affected files: `.github/workflows/ci.yml` (job `check`, sólo el paso de
  test x86), `.github/workflows/ci-x86-probe.yml` (throwaway, se borra antes
  de fusionar), posiblemente `crates/rayd/tests/**` si la causa está ahí,
  `docs/research/2026-10-ci-x86-freeze.md` (nuevo), `CHANGELOG.md`.
