# Ejemplo de `langgraph-api`

Un grafo de LangGraph servido con `langgraph dev`, sin modelo: contesta al
último mensaje en mayúsculas y, si la entrada trae `ticket_id`, lo repite. Lo
usan la prueba local y la aceptación del protocolo `langgraph-api`
(ADR-033 D5):

```bash
rayito agent deploy testdata/agent-langgraph-api/sample --protocol langgraph-api
```

`langgraph-cli[inmem]` trae el paquete `langgraph-api` (licencia Elastic-2.0)
y es un servidor de desarrollo en memoria. Se bloquea con `uv lock`.
