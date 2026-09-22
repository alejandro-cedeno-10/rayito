# Cold start, ráfagas, resume y tamaño de snapshot — 2026-09-16

Benchmark de M6 (Track D, `openspec/changes/m6-benchmark-pool`). Cada número
de este informe sale de un fichero crudo de `docs/benchmarks/raw/` (la clave
del `summary` se cita entre paréntesis) o de una consulta a Cost Explorer
citada con su comando. Script: `scripts/bench_cold_start.py`; tablas
reproducibles con `--report <json>`.

## 1. Setup

| | |
|---|---|
| Fecha / región / cuenta | 2026-09-16 (17:14–17:34 UTC), `us-east-1`, cuenta de pruebas |
| Cliente | el puesto habitual del desarrollador (Windows 11, Python 3.12.10, `rayito` 0.0.5, boto3 1.43.94, grpcio del SDK); `client_rtt_ms` = **85.5 ms** (run principal) / 97.4 ms (run e-2.0): mediana de cinco `Health` sobre la primera VM lista |
| Agente | `rayd` 0.1.0 (`Health.agent_version`) dentro de `rayito-base` **10.0** (rayd 4 533 432 B, artefacto `rayd-fc0df3c41a28.zip`) |
| Sujeto full | `rayito-base` 10.0: memoria **919 146 496 B**, code install 1 290 379 264 B, disco 37 462 016 B, `chipsetGeneration` 3, build 215,8 s (M5) |
| Sujeto slim 1.0 | `rayito-base-slim` 1.0: el artefacto de 10.0 + marcador `warmup_variant=slim` + `0004_warmup.py` (lee el marcador) + `0001_charts.py` (registro diferido por `for_type_by_name`, que resultó no funcionar en el kernel real, §10). `0002_data.py` seguía importando pandas al arrancar. Memoria **750 694 400 B**, code install 1 291 976 704 B, disco 35 332 096 B, `chipsetGeneration` 4, build 195,4 s, artefacto `rayd-68b9bb092c3e.zip` |
| Sujeto slim 2.0 | `rayito-base-slim` 2.0: el artefacto de 10.0 + marcador + los tres scripts de arranque perezosos (`0001_charts.py`, `0002_data.py` resuelven el tipo por nombre en el `lookup`; `0004_warmup.py` lee el marcador). El kernel arranca **sin** numpy, pandas ni matplotlib. Memoria **693 841 920 B**, code install 1 289 314 304 B, disco 31 604 736 B, `chipsetGeneration` 3, build 174,8 s, artefacto `rayd-a4b0266acee7.zip` |
| Resolución | sonda `Health` propia del bench a 100 ms fijos. **En los dos runs de este informe la resolución real no fue de 0,2 s**: la sonda reutilizaba las opciones de canal del SDK (`grpc.initial/min/max_reconnect_backoff_ms` 500 / 500 / 2 000 ms) y un plazo de 1,0 s por RPC, así que un primer intento de conexión rechazado o una petición retenida por el proxy costaba 0,5–2 s. Medido sobre los crudos: el periodo medio entre sondeos por muestra, `(kernel_ready_s − api_s − token_s) / health_polls`, fue p50 0,28 / p95 0,37 s (max 0,49) en (a), 0,26 / 0,34 (max 0,61) en sdk 20, 0,26 / 0,50 en raw 20 y 0,32–0,34 / 0,67–0,69 s en (e); `agent_ready_s` aparece cuantizado en escalones de ≈ 1 s (1,65–2,0 · 2,6–2,8 · 3,0 · 3,8 s). **La incertidumbre de cada valor de readiness de §2–§6 es por tanto de hasta ≈ 0,4–0,5 s en (a)/(b) y ≈ 0,7 s en (e)** (un periodo de sondeo + un RTT), no 0,2 s. Corregido tras el run (design, amendment 10): backoff de reconexión `initial`/`max` a 100 ms (`min` se queda en 500 ms: grpc-core lo usa también como timeout de conexión y a 100 ms ningún handshake TLS completa por el proxy — cinco lanzamientos `not_ready` al probarlo), plazo de 0,5 s por RPC, y cada punto de readiness registra el hueco desde que volvió el sondeo anterior (`agent_ready_gap_s`, `kernel_ready_gap_s`, `resume_gap_s`). Run de validación 201841Z (5 full + 5 slim, §10): `agent_ready_gap_s` p50 0,21 / p95 0,22 s (full) y 0,30 / 0,61 s (slim), `kernel_ready_gap_s` 0,20 / 0,22 y 0,19 / 0,29 s, es decir ≈ 100 ms + un RTT salvo cuando el proxy retiene una petición hasta el plazo (0,61 s). `pause_s` hereda la resolución de 0,5 s del sondeo de `get-microvm` del SDK (`wait_for_state`). El propio `create()` del SDK devuelve hasta un intervalo de su sondeo (0,25 s doblando hasta 2 s) después del `kernel_ready_s` de aquí; los `kernel_ready_s` de M4/M5 se midieron con ese sondeo |
| Ficheros crudos | `raw/2026-09-cold-start-20260916T171432Z.json` (fases a, b, c y e contra slim **1.0**; 112 lanzamientos, 20 ciclos, 8 min 20 s de pared) y `raw/2026-09-cold-start-20260916T173200Z.json` (fase e contra slim **2.0**; 20 lanzamientos, 1 min 35 s). `raw/2026-09-cold-start-20260916T201841Z.json`: run de validación de la sonda corregida (5 full + 5 slim 2.0, 1 min 5 s; sólo se citan sus campos `*_gap_s`, §1 y §10). `raw/2026-09-cost-explorer-2026-09-16.json` (consulta D12 del día) |
| Lanzamiento | `build_launch_plan(... timeout=600, idle=None salvo la VM auto de (c), ingress=["ALL_INGRESS"], logging="disabled", sin execution role)`; token para el puerto 8080 de 60 min; `Sandbox.connect()` público tras el `kernel_ready`; `terminate-microvm` en `finally` |

Toda VM del run está `TERMINATED` (`list-microvms` de ambas imágenes al
final: `rayito-base` 157 `TERMINATED`, `rayito-base-slim` 45 `TERMINATED`,
ninguna viva). Ningún `over_budget`, ningún `error`, `throttled_polls` 0 en
todas las fases.

## 2. (a) Creación secuencial — full (`phases.sequential`, 20 muestras)

| métrica | n | p50 | p95 | min | max |
|---|---|---|---|---|---|
| `api_s` | 20 | 0,217 | 0,271 | 0,190 | 0,292 |
| `token_s` | 20 | 0,130 | 0,143 | 0,123 | 0,185 |
| `agent_ready_s` | 20 | **2,328** | 3,021 | 1,645 | 3,798 |
| `kernel_ready_s` | 20 | **5,199** | **6,072** | 1,786 | 10,164 |
| `agent_uptime_ms_at_ready` | 20 | 23 174 | 23 951 | 22 396 | 24 602 |
| `health_polls` | 20 | 17 | 22 | 3 | 37 |
| `first_cell_s` | 20 | 0,099 | 0,106 | 0,090 | 0,107 |
| `stack_cell_s` | 20 | 0,142 | 0,151 | 0,121 | 0,153 |
| `vm_alive_s` | 20 | 6,433 | 7,130 | 5,254 | 11,586 |

Distribución de `kernel_ready_s` (ordenada): 1,79 · 4,20 · 4,43 · 4,48 ·
4,54 · 4,72 · 4,93 · 5,02 · 5,03 · 5,03 · 5,36 · 5,43 · 5,45 · 5,57 · 5,67 ·
5,90 · 5,93 · 6,01 · 6,07 · 10,16 s. El agente responde a los ≈ 2,3 s; los
≈ 2,9 s siguientes son el reinicio del kernel por defecto en `/run` (M4
D11), que vuelve a ejecutar los scripts de arranque, warm-up incluido.
`agent_uptime_ms_at_ready` ≈ 23 s en todas: es el uptime monotónico de
`rayd` que el snapshot arrastra desde la VM de build (edad del snapshot +
restore), no la latencia del restore.

Comparación: M0 Q2 (imagen sonda de 672 MB, HTTP, sin kernel): primer 200
p50 1,85 s / p95 2,13 s → aquí `agent_ready_s` p50 2,33 / p95 3,02 s con
919 MB y gRPC por el proxy. M4 Q35 (8.0, sondeo del SDK): `kernel_ready`
p50 6,22 s → aquí 5,20 s con una resolución de ≈ 0,4 s por valor (§1; el
sondeo del SDK añade hasta 2 s).

## 3. (b) Ráfagas (`phases.bursts`, orden sdk 5 → raw 5 → sdk 10 → raw 10 → sdk 20 → raw 20, 30 s entre lotes)

| mode | size | ok | throttled | failed | `batch_wall_s` | `api_s` p50/p95 | `agent_ready_s` p50/p95 | `kernel_ready_s` p50/p95 | `kernel_ready_from_batch_s` max | `throttled_polls` |
|---|---|---|---|---|---|---|---|---|---|---|
| sdk | 5 | 5 | 0 | 0 | 5,522 | 0,838 / 0,845 | 2,216 / 2,683 | 5,149 / 5,519 | 5,522 | 0 |
| raw | 5 | 5 | 0 | 0 | 7,085 | 1,950 / 1,977 | 3,705 / 4,410 | 6,064 / 7,082 | 7,085 | 0 |
| sdk | 10 | 10 | 0 | 0 | 6,618 | 0,428 / 1,206 | 1,965 / 3,643 | 4,747 / 6,613 | 6,618 | 0 |
| raw | 10 | 10 | 0 | 0 | 5,909 | 0,499 / 0,778 | 1,952 / 3,216 | 4,378 / 5,902 | 5,909 | 0 |
| sdk | 20 | 20 | 0 | 0 | 10,391 | 1,325 / 3,038 | 3,048 / 4,839 | **5,676 / 8,567** | 10,391 | 0 |
| raw | 20 | 20 | 0 | 0 | 5,942 | 0,703 / 1,200 | 2,133 / 3,595 | **5,069 / 5,687** | 5,942 | 0 |

Lectura raw frente a sdk:

- **AWS no rechazó ninguna llamada**: 0 `ThrottlingException` en 5, 10 y 20
  `run-microvm` simultáneos sin bucket ni reintentos (70 raw, 70 sdk, 140/140
  OK). Igual que M0 Q3 (20/20, 0 429). Por encima de 5 TPS la API **responde
  más despacio** en vez de rechazar: en raw 20 la mitad de las llamadas
  tardó ≈ 0,2 s y la otra mitad ≈ 1,2 s (`api_s` bimodal 0,20–0,25 /
  1,16–1,20 s); en raw 5 las cinco tardaron ≈ 1,95 s; en raw 10, 0,24–0,78 s.
- **El bucket de 5 TPS del SDK es lo que convierte la ráfaga de 20 en 8,6 s
  de p95**: las 20 llamadas sdk salen escalonadas por el `TokenBucket`
  (`api_s` de 0,20 a 3,20 s, es decir ≈ 3 s de cola para la última), y ese
  tiempo de cola entra en el `kernel_ready_s` de cada `create()`. Sin bucket
  (raw 20) el p95 es **5,69 s** y las 20 VMs están usables a los **5,94 s**
  de la primera llamada (`batch_wall_s`), frente a 10,39 s con bucket.
- Con 20 VMs sondeadas a 10 `Health`/s cada una (≈ 200 RPS al proxy) no hubo
  ningún 429 (`throttled_polls` 0). Los 13 avisos `Connection pool is full`
  del log son del pool urllib3 (10 conexiones) del cliente boto3 raw con 20
  hilos: se descartan conexiones, no peticiones.

## 4. (c) Resume (`phases.resume`, 10 ciclos por VM, las dos VMs en paralelo)

Explícito (VM `idle=None`; `pause()` → 5 s → `resume(wait=False)` + sonda
hasta la generación nueva con `kernel_ready` → `get_health()` → `run_code("x")`):

| métrica | n | p50 | p95 | min | max |
|---|---|---|---|---|---|
| `pause_s` | 10 | 1,017 | 1,039 | 1,007 | 1,039 |
| `resume_api_s` (`resume(wait=False)`: `resume-microvm` + reacuñar el JWE) | 10 | 0,271 | 0,296 | 0,264 | 0,296 |
| `resume_s` | 10 | **0,376** | **0,400** | 0,369 | 0,400 |
| `first_cell_after_resume_s` | 10 | 0,105 | 0,107 | 0,105 | 0,107 |

Auto (VM `IdlePolicy(max_idle_seconds=60, suspended_duration_seconds=600,
auto_resume=True)`; `pause()` → 5 s → `commands.run("echo back")` →
`run_code("x")`):

| métrica | n | p50 | p95 | min | max |
|---|---|---|---|---|---|
| `pause_s` | 10 | 1,012 | 1,028 | 1,003 | 1,028 |
| `auto_resume_s` | 10 | **0,666** | **0,682** | 0,658 | 0,682 |
| `first_cell_after_resume_s` | 10 | 0,100 | 0,101 | 0,099 | 0,101 |

`kernel_alive` **20/20** (`x` → `42` en todos los ciclos); `resume_generation`
1, 2, …, 10 en las dos VMs (una subida por ciclo, ninguna doble); ningún
`not_suspended`. Las dos VMs vivieron 73,2 y 76,2 s (`vm_alive_s`).
Comparación: Q39 (cuatro ciclos) 0,37 / 0,73 s → aquí p50 0,376 s con p95
0,400 s; Q40 (tres pasadas) 0,67–0,68 s → aquí p50 0,666 / p95 0,682 s;
`pause()` → `SUSPENDED` 1,37–1,49 s en M5 → 1,01–1,04 s aquí (el SDK sondea
`get-microvm` cada 0,5 s).

## 5. (d) Primera celda

| momento | métrica | p50 | p95 | fuente |
|---|---|---|---|---|
| tras `create()` (full, secuencial) | `first_cell_s` (`1+1`) | 0,099 s | 0,106 s | `phases.sequential` |
| tras `create()` (full, ráfagas 5–20) | `first_cell_s` | 0,099 s | 0,101–0,107 s | `phases.bursts[*]` |
| tras `create()` (slim 1.0 / 2.0) | `first_cell_s` | 0,099 / 0,100 s | 0,105 / 0,107 s | `phases.sequential_slim` |
| tras `resume()` explícito | `first_cell_after_resume_s` (`x`) | 0,105 s | 0,107 s | `phases.resume.explicit` |
| tras auto-resume | `first_cell_after_resume_s` | 0,100 s | 0,101 s | `phases.resume.auto` |
| celda "stack" full (pandas + matplotlib PNG) | `stack_cell_s` | 0,142 s | 0,151 s | `phases.sequential` |
| celda "stack" slim 1.0 (pandas ya cargado, matplotlib no) | `stack_cell_s` | 0,438 s | 0,446 s | run 171432Z |
| celda "stack" slim 2.0 (nada cargado) | `stack_cell_s` | 0,789 s | 0,815 s | run 173200Z |

Una celda trivial cuesta un RTT (≈ 0,1 s) en cualquier estado: el kernel no
añade latencia medible tras el create ni tras el resume. La celda con el
stack cuesta 0,14 s cuando los módulos ya están en el kernel y 0,79 s cuando
hay que importarlos (0,44 s con numpy y pandas cargados): son ≈ 0,65 s de
importación desde el disco restaurado perezosamente, que `/validate` sí
prefetcheó (Q35: sin prefetch, 45 s).

## 6. (e) Tamaño de snapshot frente a segundos

| | full 10.0 | slim 1.0 | slim 2.0 |
|---|---|---|---|
| `memorySnapshotSizeInBytes` | 919 146 496 | 750 694 400 | 693 841 920 |
| `codeInstallSizeInBytes` | 1 290 379 264 | 1 291 976 704 | 1 289 314 304 |
| build (s) | 215,8 | 195,4 | 174,8 |
| `chipsetGeneration` (VM de build) | 3 | 4 | 3 |
| en el kernel al arrancar | numpy, pandas, matplotlib, scipy, sklearn + PNG | numpy, pandas | nada |
| `agent_ready_s` p50 / p95 | 2,328 / 3,021 | 1,878 / 2,753 | 1,843 / 2,663 |
| `kernel_ready_s` p50 / p95 | **5,199 / 6,072** | 3,114 / 3,659 | **2,986 / 3,406** |
| `first_cell_s` p50 | 0,099 | 0,099 | 0,100 |
| `stack_cell_s` p50 / p95 | **0,142 / 0,151** | 0,438 / 0,446 | **0,789 / 0,815** |
| lectura de snapshot por lanzamiento (`bytes / 1e9 × 0,00155`) | $0,001 425 | $0,001 164 | $0,001 075 |
| `agent_uptime_ms_at_ready` p50 | 23 174 | 23 482 | 12 656 |

Δ de `kernel_ready_s` p50 por 100 MB de memoria, interpolación entre dos
puntos: **0,98 s / 100 MB** entre 10.0 y slim 2.0 (2,21 s por 225 MB), 1,24 s /
100 MB entre 10.0 y slim 1.0, y sólo 0,22 s / 100 MB entre slim 1.0 y 2.0. La
relación no es lineal con el tamaño: `agent_ready_s` (restore + arranque de
`rayd`) mejora 0,45–0,49 s de p50 entre 919 y 694 MB, pero esa diferencia
está por debajo de la resolución demostrada de estos runs (hasta ≈ 0,4–0,7 s
por valor, §1), así que **este dato no da una pendiente de restore por 100 MB**
(el "≈ 1 s por 500 MB" de AWS ni se confirma ni se refuta); lo que sí resuelve
es que el restore no domina: el resto de la diferencia —≈ 1,7 s, muy por
encima de la resolución— es lo que el reinicio del kernel en `/run` ejecuta:
el warm-up entero (numpy, pandas, matplotlib, scipy, sklearn, un PNG, un
`describe()`, un `inv`) en la imagen full, nada en la slim. Es decir, **el warm-up cuesta ≈ 2,2 s en cada
`create()` y compra 0,65 s en la primera celda que use el stack** (0,79 →
0,14 s), más $0,000 35 de lectura por lanzamiento y ≈ 225 MB × $0,08/GB-mes
de storage por versión. En la ráfaga de 20 el ahorro sería del mismo orden
(no medido: la fase (b) sólo corrió contra la full).

## 7. Coste del run

Bloque `cost` de los dos ficheros (a precios de `AWS_API_NOTES.md` §12, sin
storage de imagen):

| | run 171432Z | run 173200Z | smoke + comprobaciones manuales |
|---|---|---|---|
| lanzamientos (`launches`) | 112 (92 full + 20 slim 1.0) | 20 (slim 2.0) | 10 (6 full, 3 slim 1.0, 1 slim 2.0) |
| suspend / resume | 20 / 20 | 0 / 0 | 4 / 4 |
| `vm_seconds` | 823,7 | 92,5 | ≈ 200 |
| lectura esperada (GB) | 117,96 | 13,88 | ≈ 12 |
| escritura esperada (GB) | 18,38 | 0 | ≈ 3,7 |
| estimación ($) | 0,2815 | 0,0247 | ≈ 0,04 |

Total del día para Track D: **142 lanzamientos, 24 suspends, 24 resumes,
≈ 1 120 VM-segundos, ≈ 144 GB leídos, ≈ 22 GB escritos → ≈ $0,35** de uso,
más dos versiones de imagen (`rayito-base-slim` 1.0 y 2.0: ≈ 2,05 GB cada una
× $0,08/GB-mes × 7 días mínimos ≈ $0,04 cada una) y dos consultas a Cost
Explorer ($0,01 cada una): **≈ $0,45**, frente a la estimación pesimista del
`--dry-run` ($1,47) y al presupuesto de $5. La lectura de snapshot es el 80 %
del coste (0,22 de 0,28 $ en el run principal); el cómputo de 112 VMs que
vivieron 5–12 s cada una es $0,03.

## 8. Verificación de `AWS_API_NOTES.md` §12 en Cost Explorer

Consulta (`raw/2026-09-cost-explorer-2026-09-16.json`, 16:56 UTC, repetida a
las 17:34 UTC con el mismo resultado):

```
aws ce get-cost-and-usage --time-period Start=2026-09-14,End=2026-09-17 \
  --granularity DAILY --metrics UnblendedCost UsageQuantity \
  --group-by Type=DIMENSION,Key=USAGE_TYPE \
  --filter '{"Dimensions":{"Key":"SERVICE","Values":["AWS Lambda"]}}'
```

Los ficheros crudos se versionan filtrados a los usage types
`Lambda-MicroVM-*`: el resto de `AWS Lambda` en la cuenta de medida no es de
Rayito.

Los tres días siguen marcados `Estimated`; el 2026-09-16 sólo contiene las
primeras horas (761 GB-s, 16,49 GB leídos, sin línea de escritura), anteriores
al bench. Recomprobado en la aceptación de M6 (21:10 UTC, `End=2026-09-18`,
`raw/2026-09-cost-explorer-2026-09-16T2115Z.json`): la línea del 2026-09-16
sigue idéntica ($0,0487 en cuatro usage types) y el 2026-09-17 está vacío, así
que las tareas 5.1 y 5.3–5.5 quedan **diferidas al 2026-09-17** con esta
consulta como plantilla; la DECISIÓN de §9 no depende de ellas. **Precios unitarios implícitos** (USD ÷ cantidad, 2026-09-15):

| Usage type | cantidad | USD | implícito | §12 | Δ |
|---|---|---|---|---|---|
| `Lambda-MicroVM-Memory-GB-Second-ARM` | 6 177,505 GB-s | 0,022651 | $0,000 003 667 | $0,000 003 6667 | 0,00 % |
| `Lambda-MicroVM-vCPU-Second-ARM` | 3 262,989 vCPU-s | 0,090367 | $0,000 027 694 | $0,000 027 6944 | 0,00 % |
| `Lambda-MicroVM-Snapshot-Read-GB` | 113,489 GB | 0,175541 | $0,001 546 8 | $0,001 55 | −0,21 % |
| `Lambda-MicroVM-Snapshot-Write-GB` | 2,540 GB | 0,009647 | $0,003 797 7 | $0,003 8 | −0,06 % |
| `Lambda-MicroVM-Snapshot-Storage-GB-Hour` | 115,845 GB-h | 0,012872 | $0,000 111 11/GB-h = $0,08/GB-mes a 720 h | $0,08/GB-mes | 0,00 % |

Los cinco precios de §12 son correctos (AWS usa un mes de 720 h para el
storage). Ratio memoria-GB-s ÷ vCPU-s: 1,893 (09-15) y 1,874 (09-16 parcial),
no 2,0. Lo que queda para la pasada del día siguiente (tareas 5.x), con las
cantidades del 2026-09-16 completas y este run como referencia (142
lanzamientos, 24 suspends, 24 resumes, tamaños de la tabla de §6, ≈ 1 120
VM-s; el e2e de M5 de la mañana aporta además ≈ 7 lanzamientos y 4 ciclos de
10.0):

1. GB leídos por lanzamiento (`Snapshot-Read-GB / (L + R)` ponderado por
   variante) frente a `memorySnapshotSizeInBytes` (GB o GiB, páginas extra) →
   $ real por lanzamiento de 10.0 y de las slim.
2. GB escritos por suspend (`Snapshot-Write-GB / 24`); los 2,54 GB del
   2026-09-15 son los cuatro ciclos de M0 sobre 672 MB (0,635 GB por suspend
   ≈ 0,95 × la memoria, o 1,01 × si AWS cuenta GiB). El coste de un ciclo a
   10.0 sería ≈ 0,919 × (0,0038 + 0,00155) = **$0,0049**, no los $0,0107 de §12
   (que suponían 2 GB); break-even de `max_idle_seconds` ≈ 0,0049 / 0,000 035
   ≈ **140 s**, no 5 min.
3. Ratio memoria/vCPU y VM-segundos facturados frente a los ≈ 1 120 del JSON.
4. Si los dos builds de `rayito-base-slim` aparecen en alguna línea MicroVM
   y cuánto sube la línea de storage el 2026-09-17 (≈ 2 × 2,05 GB × 24 h ≈
   98 GB-h más) y el 2026-09-23 (mínimo de una semana).
5. El baseline de storage del 2026-09-14 (67,5 GB-h antes de la primera
   imagen Rayito): `list-microvm-images` sólo devuelve `rayito-base`,
   `rayito-m0-probe` y ahora `rayito-base-slim`, así que se registra como uso
   compartido de la cuenta no atribuible y el storage Rayito se calcula como
   delta sobre él.
6. Q8 (1 h activa + 7 h suspendida, 2 GB) derivada de los precios
   verificados y los tamaños medidos: 3600 × (0,000 027 6944 + 2 ×
   0,000 003 6667) + 0,919 × 0,00155 (lectura al lanzar) + 0,919 × 0,0038
   (escritura al suspender) + 0,919 GB × 7 h × $0,08/720 (storage suspendido)
   + 0,919 × 0,00155 (lectura al reanudar) = 0,1261 + 0,0014 + 0,0035 +
   0,0007 + 0,0014 = **$0,133** (§16 Q8 decía ≈ $0,142 con 2 GB de snapshot).

## 9. DECISIÓN

Regla fijada en `design.md` D11 antes de medir: `B20 =
phases.bursts[mode=sdk, size=20].summary.kernel_ready_s.p95` y `R =
phases.resume.explicit.summary.resume_s.p95`; **`B20 < 8,0 s` y `R < 2,0 s` ⇒
sin pool en v0.1; en otro caso ⇒ pool** (pre-calentado sólo si además `R ≥
2 s`; si no, pool de suspendidos).

- **`B20` = 8,567 s** (p50 5,676 s; run 171432Z).
- **`R` = 0,400 s** (p50 0,376 s).

`B20 ≥ 8,0 s` ⇒ **pool**. `B20` supera el umbral por 0,57 s, no más que la
incertidumbre de medida de ese valor (§1: en el lote sdk 20 el periodo de
sondeo fue p95 0,34 / max 0,61 s, más un RTT ≈ 0,09 s; es además un p95 sobre
20 muestras): la regla se aplica tal cual, como estaba fijada antes de medir,
pero el margen está dentro de la resolución del bench y un run con la sonda
corregida podría caer a cualquier lado del umbral. Como `R < 2 s`, el pool
decidido es el **pool de suspendidos**, no un pool de VMs `RUNNING` a $0,126/h cada una: la
reanudación explícita tarda 0,40 s y el auto-resume 0,68 s, así que una VM
aparcada suspendida cuesta storage ($0,08/GB-mes × 0,92 GB ≈ $0,07 al mes) y
entrega un sandbox usable en < 1 s. Queda registrado como **ADR-008 "Pool de
MicroVMs"** en `ARCHITECTURE.md`; `SPEC.md` §4 deja de listar el pool como
no-objetivo; la implementación es el cambio OpenSpec `m7-suspended-pool`,
propuesto en el ADR y no incluido aquí.

Contexto que el ADR recoge y que un lector debe tener al lado de la regla:

- Los p50 están lejos de los umbrales: 5,68 s en la ráfaga sdk de 20, 5,20 s
  secuencial, 0,38 s de resume.
- De los 8,57 s de `B20`, ≈ 3,0 s son la cola del **bucket de 5 TPS del
  propio SDK** (`api_s` p95 3,04 s en sdk 20 frente a 1,20 s en raw 20). La
  misma ráfaga sin bucket (raw 20) queda en **p95 5,69 s**, bajo el umbral, y
  AWS no ha rechazado ninguna de las 140 llamadas en ráfaga de este run ni
  de M0. La cuota publicada sigue siendo 5 TPS (§11): relajar o retirar el
  bucket es una decisión aparte que el cambio del pool debe medir (una
  ráfaga de 20 cada pocos segundos, no una sola).
- La imagen slim 2.0 (sin warm-up en el reinicio de `/run`) baja el
  secuencial a p50 2,99 / p95 3,41 s: la mayor parte del cold start actual es
  el warm-up que el kernel repite en cada `create()`, no el tamaño del
  snapshot (§6). Ajustar el warm-up es un cambio de imagen (Track A), no del
  pool.
- E2B declara ≈ 1,6 s en ráfaga; sin pool Rayito da 5–6 s (p50) y con el pool
  de suspendidos ≈ 0,7 s en el primer request.

Diseño del pool de suspendidos (esquema que el ADR fija y `m7-suspended-pool`
detalla): el SDK (lado cliente, sin servicio) mantiene N sandboxes creados
por adelantado con `IdlePolicy(auto_resume=True)` y pausados nada más llegar
a `kernel_ready`; `Sandbox.create()` toma uno y su primera petición lo
reanuda (`auto_resume_s` ≈ 0,67 s medido). Coste aparcado = storage del
snapshot de la VM suspendida (≈ $0,07 por VM y mes a 0,92 GB) + una
escritura por aparcado ($0,0038/GB) + una lectura por toma ($0,00155/GB).
Restricciones: (1) las 8 h de `maximumDurationInSeconds` cuentan el tiempo
suspendido, así que cada plaza se recicla antes de vencer (un lanzamiento
por plaza cada < 8 h); (2) el `runHookPayload` (hash del token, `envs`) se
fija en `/run`, así que el dueño del pool acuña el access token y quien toma
la VM lo hereda: el traspaso del token dentro del proceso del SDK, o una
rotación de secreto en tiempo de `/run` que hoy no existe, es el **punto de
diseño abierto**; (3) por lo mismo, los `envs` son por pool, no por sandbox;
(4) las VMs suspendidas consumen la cuota de memoria de la región (§11) y
aparecen en `list()` como `SUSPENDED`; (5) una VM aparcada más de
`suspended_duration_seconds` se termina sola: el pool la repone.

## 10. Apéndice

Comandos (desde `clients/python`, `AWS_PROFILE=<tu-perfil>
AWS_REGION=us-east-1 RAYITO_E2E=1`):

```
uv run python ../../scripts/bench_cold_start.py \
  --template arn:aws:lambda:us-east-1:123456789012:microvm-image:rayito-base \
  --template-slim arn:aws:lambda:us-east-1:123456789012:microvm-image:rayito-base-slim \
  --dry-run
# phases=a,b,c,e sequential=20 batches=[sdk 5, raw 5, sdk 10, raw 10, sdk 20, raw 20] resume_cycles=10 slim=True
# launches=112 cycles=20 estimate=$1.47 budget=$5.00

uv run python ../../scripts/bench_cold_start.py --template <full arn> --template-slim <slim arn> \
  --out ../../docs/benchmarks/raw                # run 171432Z (slim 1.0), 17:14:32–17:22:52 UTC
uv run python ../../scripts/bench_cold_start.py --template <full arn> --template-slim <slim arn> \
  --phases e --out ../../docs/benchmarks/raw     # run 173200Z (slim 2.0), 17:32:00–17:33:35 UTC
uv run python ../../scripts/bench_cold_start.py --report ../../docs/benchmarks/raw/<file>.json

# run de validación de la sonda corregida (amendment 10), 20:18:41–20:19:46 UTC
uv run python ../../scripts/bench_cold_start.py --template <full arn> --template-slim <slim arn> \
  --phases a,e --sequential 5 --out ../../docs/benchmarks/raw   # run 201841Z
```

El run 201841Z lanzó la **última** versión de cada imagen en ese momento
(`meta.images`): `rayito-base` **15.0** (publicada por Track A después de este
bench; misma memoria 919 146 496 B, code install 1 304 760 320 B) y
`rayito-base-slim` 2.0. Sirve sólo para medir la resolución de la sonda (los
campos `*_gap_s` de §1); sus `kernel_ready_s` (full p50 5,11 s, slim 3,31 s
sobre 5 muestras) no se mezclan con §2–§6. Un primer intento con
`grpc.min_reconnect_backoff_ms` también a 100 ms dio `not_ready` en los cinco
lanzamientos (VMs `RUNNING`, ningún `Health` completado: grpc-core usa ese
argumento como timeout de conexión); el bench terminó cada VM al vencer sus
120 s y el operador terminó a mano la que quedó viva al cortar el proceso.

Imágenes: `rayito-base-slim` 1.0 y 2.0 se publicaron con
`publish_image.py --artifact <zip> --variant slim` sobre zips construidos por
`image_zip.py <dir> <zip> --variant slim`, donde `<dir>` es el artefacto de
10.0 (`rayd-fc0df3c41a28.zip`) extraído con los scripts de arranque del
árbol copiados encima (`image_zip.py` sobre ese directorio sin `--variant`
reproduce el sha256 de 10.0 byte a byte).

Celdas (constantes del script, nunca en logs):

```python
FIRST_CELL = "1+1"                       # -> "2"
STATE_CELL = "x = 42"; RESUME_CELL = "x"  # -> "42"
STACK_CELL = ('import io; import numpy as np; import pandas as pd; '
              'import matplotlib.pyplot as plt; '
              'df = pd.DataFrame({"a": [1.0, 2.0]}); fig, ax = plt.subplots(); '
              'ax.plot(df["a"]); buf = io.BytesIO(); fig.savefig(buf, format="png"); '
              'plt.close(fig); len(buf.getvalue())')   # -> int > 0
AUTO_RESUME_COMMAND = "echo back"        # -> "back"
```

Comprobación manual de las variantes (una VM cada una, `Sandbox.create` +
`import sys; sorted(m for m in ('numpy','pandas','matplotlib') if m in sys.modules)`
+ `plt.plot([1, 2, 3]); plt.show()`):

| imagen | módulos al arrancar | `image/png` | `e2b/chart` |
|---|---|---|---|
| `rayito-base` 10.0 | `['matplotlib', 'numpy', 'pandas']` | sí | `line` |
| `rayito-base-slim` 1.0 | `['numpy', 'pandas']` | sí | **no** (registro `for_type_by_name` borrado por `select_figure_formats` al primer `import matplotlib.pyplot`) |
| `rayito-base-slim` 2.0 | `[]` | sí | `line` |

Desviaciones respecto a `design.md` (recogidas en sus "Amendments"): el
sujeto full es 10.0 (no se publicó 11.0); la slim se construyó desde el
artefacto de 10.0 y no desde el árbol; `ingress=["ALL_INGRESS"]`; `token_s` es
la duración de la llamada; campos extra `resume_api_s`, `state`,
`state_reason`, `dwell_s`, `failed_launches`; los formatters `e2b/chart` y
`e2b/data` resuelven el tipo por nombre en `lookup_by_type` en vez de
`for_type_by_name`; dos versiones slim; tests unitarios en `scripts/tests/`,
`kernel-sidecar/tests/test_variant.py` y `kernel-sidecar/tests/test_formatters.py`;
la sonda corregida y los campos `*_gap_s` (amendment 10) no existían en los
dos runs principales. Números tomados el 2026-09-16 entre las 17:10 y las
17:35 UTC; validación de la sonda a las 20:18 UTC.
