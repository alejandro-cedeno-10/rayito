## Contexto

`check` (x86_64) corre sólo `--lib --bins` desde 3c671f4a porque las suites de
integración de `rayd` (17 binarios en `crates/rayd/tests/*.rs`) congelan el
runner con la fuerza suficiente para que ni `timeout --kill-after` (paso,
ebb7089) ni la cancelación del job (GitHub) respondan. Dos intentos previos
tocaron las causas más obvias sin resolver el cuelgue duro:

- 18e5b52/fd07d9b/565c49e: los procesos que lanza `rayd` para el usuario ya no
  heredan descriptores del runner más allá de stdio.
- ebb7089: salida a fichero + `timeout --kill-after` por si un proceso
  huérfano de un test retenía el paso abierto.

Como el cuelgue sobrevivió a ambos, este cambio no repite esas hipótesis
como si fueran nuevas: las trata como ya evaluadas y busca la siguiente capa
(agotamiento de recursos del runner, señales de proceso/sesión, o un test en
concreto) con evidencia en vivo en lugar de un timeout mudo.

## Por qué un workflow de sondeo separado (y no instrumentar `ci.yml` directamente)

- `ci.yml` corre en cada PR/push a `main`/`develop`; añadirle 17 jobs de
  diagnóstico ahí penalizaría cada PR del repo mientras dura la
  investigación.
- Un cuelgue duro que ni `cancel` ni `timeout` interrumpen puede consumir el
  runner hasta que GitHub lo da por perdido (varias decenas de minutos); eso
  no debe bloquear el resto de jobs de `ci.yml` en un PR real.
- Aislar en una rama descartable (`ci/x86-freeze-probe`, PR en borrador desde
  la cuenta `alejandro-cedeno-10`) permite re-ejecutar sin límite y borrar el
  workflow antes de fusionar nada a `main` — nunca debe quedar código de
  sondeo en el árbol principal.

## Árbol de decisión (fase 3)

```
¿Reprodujo el cuelgue en >= 3 corridas de la fase 1?
├─ No, y tampoco en >= 5 corridas totales
│   └─> reactivar suite completa en x86 (mismo wrapper que `arm`),
│       documentar "no reproducido" con las corridas como evidencia.
└─ Sí
    └─ Fase 2: ¿el binario aislado (`--exact`, `--test-threads=1`) también
       congela, o sólo el binario completo?
       ├─ Sólo el binario completo (aislado va bien) Y el leg con
       │  `systemd-run --scope -p TasksMax=... -p MemoryMax=...` FALLA en
       │  vez de congelar
       │   └─> agotamiento de recursos (pids/hilos/memoria) tirando del
       │       agente del runner. Causa está en cómo el test/harness lanza
       │       procesos, no en el binario del runner mismo.
       │       ¿Arreglable en tests/ (ej. limitar paralelismo, poner rlimits
       │       explícitos en el harness)?
       │       ├─ Sí -> fase 3.1 (arreglar en tests/, probar, reactivar).
       │       └─ No, la causa raíz está en cómo `rayd` src lanza/gestiona
       │          procesos (ej. `process_spawner.rs`, `pty_backend.rs`,
       │          `sidecar_process.rs` no limitan algo que sí importa bajo
       │          la contención del runner) -> fase 3.2, STOP, reportar.
       └─ El test individual también congela
           └─> el problema es ese test concreto (bucle, señal escapando,
               fd concreto). Si se arregla en el fichero de test -> 3.1.
               Si el test ejercita un comportamiento real de `rayd` que sólo
               falla bajo la contención del runner x86 -> 3.2, STOP, reportar.
```

## Qué NO hace este cambio

- No toca `crates/rayd/src/**` ni `crates/rayd-core/**` bajo ninguna rama del
  árbol de decisión: si la causa vive ahí, el resultado de este cambio es un
  reporte, no un parche.
- No cambia el job `arm`, sus timeouts, ni el paso `m9_egress` en netns.
- No mueve x86 a un runner distinto ni sube timeouts para enmascarar un
  cuelgue: un timeout más alto no es una corrección si la causa es que el
  runner deja de responder por completo.

## Resultado del árbol

Rama "el test individual también congela": sólo
`a_stalled_second_subscriber_is_truncated_alone` congela aislado (y
también sin harden-runner, hipótesis añadida porque su agente sólo corre en
x86). La fase 2b corre el mismo test con la salida recortada antes del log y
ya no congela: el problema es la línea de ~2.9 MB que imprime el panic, en el
fichero de test. Se arregla en `tests/` (fase 3.1). La pata `systemd-run
--scope` quedó inválida (sin `--gid` corre con gid 0 y `rayd` rechaza la
PTY) y no hizo falta repetirla. Detalle en
`docs/research/2026-10-ci-x86-freeze.md`.
