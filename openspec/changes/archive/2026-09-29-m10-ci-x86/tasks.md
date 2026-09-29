## 1. Sondeo (fase 1 — aislar)

- [x] 1.1 Rama `ci/x86-freeze-probe`, `.github/workflows/ci-x86-probe.yml`:
      matriz de 17 jobs (uno por binario de `crates/rayd/tests/*.rs`),
      acciones fijadas a los mismos SHA que `ci.yml`, `permissions: contents:
      read`, `fail-fast: false`, `timeout-minutes: 20` por job.
- [x] 1.2 Cada job: harden-runner (audit) → checkout → rust-cache →
      diagnóstico (`id; ulimit -a; nproc; free -m; df -h; pid_max;
      threads-max; ps --forest`) → build (`cargo test ... --no-run
      --message-format=json | jq`) → sampler de fondo cada 10 s al log vivo →
      `timeout --kill-after=15s 10m "$bin" --test-threads=2` con salida en
      vivo (`tee`, no sólo a fichero) → `if: always()` con `dmesg -T | tail
      -100` y el árbol de procesos.
- [x] 1.3 PR en borrador desde la cuenta `alejandro-cedeno-10`
      (`GH_TOKEN=$(gh auth token -u alejandro-cedeno-10)`). Ejecutar >= 3
      veces (re-run de todos los jobs) antes de sacar conclusiones.

## 2. Sondeo (fase 2 — por hipótesis, sólo si algo congeló en la fase 1)

- [x] 2.1 Por cada binario que congeló o hizo timeout: matriz por test
      individual (`$bin --list`, `$bin --exact <test> --test-threads=1` bajo
      `timeout --kill-after=10s 120s`).
- [x] 2.2 Leg adicional del mismo binario bajo `sudo systemd-run --scope
      --uid=runner -p TasksMax=4096 -p MemoryMax=10G --` (agotamiento de
      recursos contenido vs. runner completo).
- [x] 2.3 Confirmar o descartar, en este orden: descriptores heredados
      llegando a shells/procesos de test; señales de grupo de proceso/sesión
      escapando de un test; agotamiento de pids/hilos bajo el usuario del
      runner; agotamiento de memoria + OOM de `Runner.Worker`; test en
      bucle de CPU (`m6_limits`) inaniendo al agente.

## 3. Decisión y cambio real (fase 3)

- [x] 3.1 Si la causa está en el harness o en `crates/rayd/tests/**`:
      arreglarla ahí (comentario de racional), probar con >= 3 ejecuciones
      verdes de la suite completa de x86 en el workflow de sondeo, y sólo
      entonces cambiar el paso de test x86 de `check` en `ci.yml` de vuelta a
      `timeout --kill-after=30s 20m cargo test --workspace --locked
      --no-fail-fast > log 2>&1 < /dev/null` (quitar `--lib --bins`, mantener
      wrapper de fichero/timeout, actualizar el nombre del paso).
- [ ] 3.2 (no aplica: la causa está en `tests/`) Si la causa está en `rayd` src: STOP, no tocar `src/`; dejar la
      evidencia y la corrección propuesta para el orquestador en el reporte
      final (no en el PR).
- [ ] 3.3 (no aplica) Si sólo binarios concretos y no arreglable desde `tests/`:
      reactivar el resto en x86 con lista explícita
      (`cargo test -p rayd --test a --test b ... --workspace --lib --bins`),
      documentar los excluidos.
- [ ] 3.4 (no aplica: reprodujo) Si no reproduce en >= 5 ejecuciones completas: reactivar la suite
      completa en x86 con el wrapper de timeout, documentar el resultado
      negativo.
- [x] 3.5 Borrar `ci-x86-probe.yml` y cerrar el PR en borrador antes de que
      el cambio real llegue a `main`.
- [x] 3.6 `docs/research/2026-10-ci-x86-freeze.md`: ejecuciones, enlaces,
      evidencia, hipótesis descartadas, causa raíz o "no reproducido",
      decisión sobre `ci.yml`.
- [x] 3.7 `crates/rayd/CHANGELOG.md` (`## [Unreleased]`) y cualquier doc
      afectado (`MILESTONES.md` no se toca en este cambio).

## 4. Verificación

- [x] 4.1 `openspec validate m10-ci-x86 --strict`.
- [x] 4.2 `actionlint` y `scripts/check_pins.py` verdes.
- [ ] 4.3 PR final: job `check` (x86) corre y pasa la suite completa dentro
      de su límite de 25 min por paso; job `arm` sin diff (`git diff` del
      bloque `arm:` vacío) y verde.
- [x] 4.4 Si se tocó `crates/rayd/tests/**`: suite completa en la VM Lima
      aarch64 como `tester` (uid 1500), más `m9_egress` en netns como root.

## Resultado

Causa raíz en `crates/rayd/tests/m5_pty.rs`
(`a_stalled_second_subscriber_is_truncated_alone`): búsqueda cuadrática que
agotaba el plazo de 30 s en x86 y un panic que imprimía ~2.9 MB en una sola
línea al log del runner. Evidencia, corridas e hipótesis descartadas en
`docs/research/2026-10-ci-x86-freeze.md`.
