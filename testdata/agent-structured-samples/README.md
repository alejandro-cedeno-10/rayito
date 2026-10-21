# Ejemplos de entrada estructurada

Agentes de ejemplo de la entrada estructurada y la selección de grafo
(ADR-033 D3). Viven fuera de `testdata/agent-samples/` porque aquella carpeta
es la lista cerrada de los dieciséis ejemplos que la aceptación de
`agent-zero-config` despliega sin opciones, y este no se despliega sin
`--graph`.

- `python/langgraph-structured/`: `langgraph.json` con dos grafos, `triage` y
  `echo`, sin ninguno llamado `agent`. `triage` recibe `{"ticket_id": ...}` y
  devuelve `priority` y `summary`; `echo` devuelve el `text` que recibe. Los
  nodos son deterministas: no llaman a ningún modelo. `rayito-agent.toml` sólo
  declara el modelo (el runtime lo detecta Rayito). Se bloquea con `uv lock`.
- `expected/python-langgraph-structured.json`: el proyecto que resulta de
  `AgentProject.detect(..., DetectOverrides(graph="triage"))` en la región
  `us-east-1`; lo comparan los tests de Python y de TypeScript.
