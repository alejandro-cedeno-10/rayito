# vercel-ai-bot

Contexto de build exportado por Rayito (`AgentProject.export()`). El contenedor
sirve el contrato `rayito.agent-service/1` en el puerto 8080: `GET /ping` y
`POST /invocations`.

## Construir y probar en local

```bash
docker buildx build --platform linux/arm64 -t vercel-ai-bot .
docker run --rm --platform linux/arm64 -p 8080:8080 \
  -e RAYITO_AGENT_SERVICE_TOKEN=<token> vercel-ai-bot
curl http://127.0.0.1:8080/ping
curl -X POST http://127.0.0.1:8080/invocations \
  -H 'Authorization: Bearer <token>' -H 'Content-Type: application/json' \
  -d '{"prompt": "hola"}'
```

Con `Accept: text/event-stream` la respuesta llega como un mensaje SSE por evento.
`RAYITO_AGENT_SERVICE_TOKEN` exige `Authorization: Bearer <token>` en `/invocations`.

## Credenciales

Fuera de un sandbox de Rayito no hay pasarela de secretos: la credencial del
modelo la pone la plataforma donde corras el contenedor (variables de entorno o
rol de IAM) y el agente puede leerla. La garantía de que el agente nunca ve la
credencial sólo vale dentro de Rayito.

## Amazon Bedrock AgentCore Runtime

AgentCore espera una imagen `linux/arm64` que sirva `/ping` y `/invocations`
en el puerto 8080, como esta. La forma más corta de desplegarla es
`rayito agent deploy <directorio> --target agentcore`: construye la imagen con
CodeBuild (sin Docker local), la sube a Amazon ECR, crea el rol del runtime con
el mínimo privilegio y crea o actualiza el runtime. A mano: sube la imagen a ECR,
crea el runtime en la consola o con la CLI de AgentCore y activa MMDSv2
(`requireMMDSV2` en `UpdateAgentRuntime`), sin el que AgentCore rechaza las
invocaciones. AgentCore autentica las invocaciones por sí mismo (SigV4): no
fijes `RAYITO_AGENT_SERVICE_TOKEN`.

El argumento de build `AGENT_BASE_IMAGE` toma la misma imagen base, con el mismo
digest, de otro registro (por ejemplo `public.ecr.aws/docker/library/python`).

### Seguridad: sin pasarela de secretos

En AgentCore no hay pasarela de secretos de Rayito: el agente obtiene las
credenciales del rol del runtime por el servicio de metadatos de la microVM, y
cualquier código que corra en la sesión puede leerlas. Da al rol sólo lo que el
agente necesita: los modelos de Bedrock que usa y, si hace falta, los secretos
que debe leer.
