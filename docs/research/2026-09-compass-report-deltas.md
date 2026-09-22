# Informe Compass ("SDK tipo E2B sobre Lambda MicroVMs") — qué se toma y qué se corrige

Origen: `compass_artifact_wf-83a6cb41-…_text_markdown.md` (informe previo del que
salieron `SPEC.md` y `ARCHITECTURE.md`). Contrastado el 2026-09-14/15 contra el
modelo de servicio, la documentación oficial y el código de E2B/aws-samples
(`.claude/research/`, `AWS_API_NOTES.md`).

## Se mantiene (confirmado)

- Producto real: AWS Lambda MicroVMs, GA 2026-06-22, Firecracker, ARM64/Graviton,
  endpoint HTTPS por MicroVM, JWE en `X-aws-proxy-auth`, suspend/resume, 8 h máx.
- Arquitectura de tres capas: imagen (Dockerfile → snapshot) + agente interno tipo
  `envd` + SDK generado desde `.proto`. Una imagen por template.
- Kernel Jupyter persistente para `run_code` con resultados enriquecidos.
- Inventario de features de E2B por módulo: base del contrato `.proto`.
- Riesgo de credenciales del execution role a través de suspend/resume: sigue
  siendo pregunta 1 de M0 (`AWS_API_NOTES.md` §16).
- Hueco de mercado: SDK open-source, serverless, dentro de la cuenta del cliente,
  sin clúster. Sigue vigente con matices (abajo).

## Se corrige (datos que cambiaron o eran imprecisos)

| Informe Compass | Verificado 2026-09-14 | Fuente |
|---|---|---|
| 5 regiones | **10** (+ Mumbai, Singapur, Sídney, Fráncfort, Estocolmo, ago-2026); PrivateLink en todas | whats-new 2026-08 |
| Hooks `/run`, `/resume`… sin ruta | `POST /aws/lambda-microvms/runtime/v1/<hook>` en `hooks.port` (sin default; 9000 por convención); timeouts runtime 1–60 s | docs launching + modelo |
| CMD arranca en 8080 y ahí van los hooks | gRPC en 8080; hooks en puerto propio fuera de `allowedPorts` | AWS_API_NOTES §8 |
| Auth interna "como envd, /init" | No hay `/init`; el canal per-VM es `runHookPayload` (≤4096, `sensitive`) en el body de `/run`. Env vars son de imagen | modelo RunMicrovm |
| "~9x Fargate spot", $3.03/día | 2 GB/1 vCPU = $0.126/h ≈ 3.2× Fargate ARM on-demand, ~10× spot, ~1.5× E2B; ciclo suspend/resume ≈ $0.011 | pricing page |
| Cold start "~2 s" | terceros: p50 2.7–3.5 s, p95 3.2–4.5 s a primera respuesta; resume 0.7–2.6 s | alchemy.run, microvm-ctl |
| `additionalOsCapabilities: ALL` = "containerd anidado" | docs: mounts, network namespaces, eBPF | docs images |
| "Nadie ocupa la intersección" | Ya hay RaitBox, asbox, microvms-agentd, microvm-ctl (0–6 ⭐, sin pause con memoria + Jupyter + gRPC). El diferencial es el kernel vivo tras pause/resume | landscape research |
| Go + Connect-RPC | ADR-001: Rust + tonic gRPC (Connect en Rust inmaduro; el proxy soporta gRPC). Requiere `x-aws-proxy-force-h2: true` | ADR-001 |
| Plano de control como Lambda Functions | Librería en el cliente (ADR en SPEC §5); un servicio se evalúa en M6 | SPEC |
| `set_timeout` MVP | Imposible: no existe `UpdateMicrovm`; duración e idle policy inmutables | modelo |
| Varios pools HTTP/2 como E2B | Cap **8 conexiones** por MicroVM a 1 vCPU; máximo 2 canales | limits page |
| `get_host(port)` → host | Un solo hostname; hace falta `X-aws-proxy-port` + token con ese puerto | docs networking |
| Tamaño elegido al lanzar | Tamaño fijado en la imagen (`resources[].minimumMemoryInMiB`) | modelo |
| Pool caliente "si cold start > 1.5–2 s" | Decidir en M0/M6 con medidas; snapshot pequeño + `/validate` primero | plan |

## Ideas del informe que entran al plan

- Benchmark de ráfaga concurrente, no secuencial (M0 pregunta 3).
- Persistencia de filesystem entre sesiones vía S3/EFS: fuera de M1–M5, candidato M6.
- Egress allowlist = conector VPC + SG restrictivo + proxy: M6 (ya en MILESTONES).
- Compatibilidad E2B declarada explícitamente con `unimplemented` (patrón Dormice):
  adoptado como regla del `.proto`/SDK.
