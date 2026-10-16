# support-bot

Contexto de build exportado por Rayito (`AgentProject.export()`). El contenedor
sirve el contrato `rayito.agent-service/1` en el puerto 8080: `GET /ping` y
`POST /invocations`.

## Construir y probar en local

```bash
docker buildx build --platform linux/arm64 -t support-bot .
docker run --rm --platform linux/arm64 -p 8080:8080 \
  -e RAYITO_AGENT_SERVICE_TOKEN=<token> support-bot
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
