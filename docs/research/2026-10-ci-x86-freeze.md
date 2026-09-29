# Cuelgue del runner x86_64 con las suites de integración de `rayd`

Investigación de M10 (`openspec/changes/m10-ci-x86`), 2026-09-29. Desde
3c671f4 el job `check` de `ci.yml` (x86_64, `ubuntu-24.04`) corría sólo
`cargo test --workspace --lib --bins`: con las 17 suites de
`crates/rayd/tests/*.rs` el runner se quedaba mudo de forma intermitente,
sin log, y ni el `timeout --kill-after` del paso (ebb7089) ni la cancelación
del job respondían. El job `arm` (`ubuntu-24.04-arm`) siempre corrió la
suite completa sin problemas.

**Resultado:** causa raíz en un fichero de test, no en `rayd`.
`a_stalled_second_subscriber_is_truncated_alone` (`crates/rayd/tests/m5_pty.rs`)
tardaba ~30 s en x86 contra un plazo propio de 30 s y, al vencer, su panic
imprimía el buffer entero: **una sola línea de ~2.9 MB** en la salida del
test. Con esa línea en el log el runner deja de responder. Arreglado en el
test y la suite completa vuelve a x86.

## Sondeo

Workflow descartable `.github/workflows/ci-x86-probe.yml` en la rama
`ci/x86-freeze-probe`, PR en borrador
[#39](https://github.com/alejandro-cedeno-10/rayito/pull/39) (cerrado sin
fusionar; el fichero no llega a `main`). Mismas acciones y SHAs que
`ci.yml`, `permissions: contents: read`, un job por pata con
`timeout-minutes: 20`. Cada job imprime un diagnóstico del runner (`id`,
`ulimit -a`, `nproc`, `free`, `pid_max`, `threads-max`, descriptores del
shell del paso, árbol de procesos), arranca un muestreador cada 10 s al log
en vivo y termina con `dmesg` y el árbol de procesos (`if: always()`).

| Fase | Corrida | Qué corre | Resultado |
|---|---|---|---|
| 1 | [36609832898](https://github.com/alejandro-cedeno-10/rayito/actions/runs/36609832898) (3 intentos) | los 17 binarios, uno por job, `--test-threads=2` bajo `timeout --kill-after=15s 10m` | 50/51 verdes. Intento 3: `m5_pty` congela. El paso sigue `in_progress` pasado el `timeout` de 10 min y los 20 min del job; GitHub lo da por cancelado a los 25 min y el log no existe (`BlobNotFound`) |
| 2 | [36615379135](https://github.com/alejandro-cedeno-10/rayito/actions/runs/36615379135) | `m5_pty`: los 16 tests aislados (`--exact`, 1 hilo, 10 repeticiones, 120 s cada una); binario completo x5 con y sin harden-runner; binario completo en `systemd-run --scope` | Congelan **sólo** `aislado a_stalled_second_subscriber_is_truncated_alone` y los 3 jobs `sin-harden`, todos igual que en la fase 1 (cancelados a los 25 min, sin log). Los otros 15 tests aislados: 150/150 verdes. `contenido`: inválido, ver abajo |
| 2b | [36618847164](https://github.com/alejandro-cedeno-10/rayito/actions/runs/36618847164) | el mismo test, mismo código, 10 repeticiones × 5 jobs, pero con la salida cruda a fichero y al log sólo líneas recortadas a 300 caracteres más la longitud de la línea más larga | **Ningún cuelgue** (5/5 jobs terminan en 3-6 min). 32 de 50 repeticiones fallan a los 30-31 s, cada una con una línea de 2.78-3.0 MB (`needle "finished\r\n" not found in "…xxxx…"`); las que pasan tardan 28-30 s. Un runner más rápido: 14-15 s y 10/10 verdes |
| 3 | [36619738136](https://github.com/alejandro-cedeno-10/rayito/actions/runs/36619738136) (intentos 1 y 2) | `m5_pty.rs` arreglado: el test aislado x10 en 3 jobs, y la suite completa de x86 con el comando nuevo de `ci.yml` en 3 jobs | El test: 1-2 s por repetición, 30/30 verdes, línea más larga 219 bytes. Suite completa: 3/3 verdes por intento (24 binarios de test, 883 tests pasan, 2 ignorados, 0 fallos, línea más larga 280 bytes, ~1 min 40 s de paso con caché) |

## Causa raíz

`read_until_with` buscaba la aguja con
`buffer.windows(needle.len()).any(..)` sobre **todo** el buffer cada vez que
llegaba un trozo. El test manda 3 MB de `x` sin salto de línea por la
terminal, que llegan en trozos de ~4 KiB, así que la búsqueda es cuadrática:
cientos de pasadas sobre hasta 3 MB en un build debug. Eso dominaba el
tiempo del test: ~21 s en el runner aarch64
([run 36609607221](https://github.com/alejandro-cedeno-10/rayito/actions/runs/36609607221),
PTY 3605: 20 971 ms), 14-31 s en los runners x86. El plazo del test es de
30 s. Cuando vence, tanto el `assert!(!remaining.is_zero(), …)` como el
`panic!` del `timeout` formatean `String::from_utf8_lossy(&buffer)`, así que
el binario de test escribe una línea de ~2.9 MB.

Esa línea llega al log del runner, tanto con `tee` (sondeo) como con el
`cat "$RUNNER_TEMP/cargo-test.log"` de `ci.yml`. Por eso ebb7089 (salida a
fichero y `timeout` duro) no lo evitó: el fichero se volcaba entero al
final. Después el runner no sube el log, no atiende el `timeout-minutes` del
job y la cancelación sólo llega por la vía de "runner perdido" de GitHub. No
tenemos visibilidad dentro de `Runner.Worker`. La evidencia es causal: la
fase 2b corre el mismo test con el mismo código y el mismo fallo a los 30 s,
cambia sólo que la línea no llega al log, y el cuelgue desaparece (0 de 5
jobs, frente a 4 de 4 en la fase 2).

En aarch64 no se veía porque el test acaba antes de su plazo (~21 s), así
que nunca imprime la línea.

## Hipótesis descartadas

| Hipótesis | Evidencia |
|---|---|
| Descriptores heredados del runner llegando a los shells/procesos del test | El shell del paso sí recibe dos pipes del `Runner.Worker` (fds 142 y 145, `Runner.Worker spawnclient 142 145`). Pero los tests `a_descriptor_without_close_on_exec_reaches_neither_shell_nor_process` y `jobs_and_processes_inherit_neither_end_of_the_terminal` pasan 10/10 aislados, y el cuelgue desaparece en la fase 2b sin tocar nada del manejo de descriptores |
| Señales de grupo de proceso o sesión escapando de un test | Todo el runner (hosted-compute-agent, `Runner.Listener`, `Runner.Worker`, pasos) comparte pgid/sid; `timeout` mete el test en su propio grupo. El test culpable sólo manda `Kill` a su propia shell PTY (su propia sesión), congela aislado y deja de congelar con sólo recortar la salida |
| Agotamiento de pids/hilos del usuario del runner | `ulimit -u` 63 838, `pid_max` 4 194 304, `threads-max` 127 677, ~170 procesos en el muestreo. El cuelgue se reproduce con un único test, un hilo y una sola shell |
| Memoria / OOM de `Runner.Worker` | 16 GB con ~14 GB disponibles, buffers de ~3 MB, sin OOM en el `dmesg` de los jobs que terminan. Mismo argumento: un test aislado basta |
| Bucle de CPU (`m6_limits`) dejando sin CPU al agente | `m6_limits` verde en las 3 corridas de la fase 1. El binario que congela es `m5_pty` |
| Agente de harden-runner (sólo x86: en arm64 imprime "ARM runners are not supported in the Harden-Runner community tier" y no instala nada) | Los 3 jobs `sin-harden` de la fase 2 congelan igual. La fase 3 pasa con harden-runner puesto |

La pata `contenido` (`sudo systemd-run --scope --uid=runner -p TasksMax=4096
-p MemoryMax=10G`) no sirvió: sin `--gid` el proceso corre con gid 0 y `rayd`
rechaza la PTY ("only unprivileged accounts of this image may run code (uid
and gid >= 1000, never in group 0)"). No se repitió porque la fase 2 ya había
aislado el test y la hipótesis de agotamiento quedaba descartada por otro
camino.

## Arreglo (`crates/rayd/tests/m5_pty.rs`)

- `read_until_with` busca sólo en los bytes nuevos, más `needle.len() - 1`
  de solape, así que la búsqueda es lineal. El test pasa de 14-30 s a 1-2 s
  en x86.
- Los cuatro mensajes de fallo que imprimían un buffer de terminal
  (`read_until_with` ×2, `read_line_after`, `collect_until_exit`) usan
  `shown(&buffer)`: longitud y últimos 512 bytes. Un fallo futuro de este
  test ya no puede meter megas en el log.

`m5_suspend_resume.rs` (`read_pty_until`) tiene el mismo patrón, pero sus
salidas son de pocos bytes y nunca congeló (3/3 en la fase 1). No se toca.
Ningún cambio en `crates/rayd/src/**`.

## Decisión sobre `ci.yml`

El paso de test del job `check` vuelve a
`timeout --kill-after=30s 20m cargo test --workspace --locked --no-fail-fast > "$RUNNER_TEMP/cargo-test.log" 2>&1 < /dev/null`
(sin `--lib --bins`, con el mismo envoltorio de fichero y timeout y el mismo
límite de 25 min del paso) y pasa a llamarse
`cargo test (whole workspace, rayd's integration suites included)`. El job
`arm` no cambia: el diff del bloque `arm:` está vacío.
