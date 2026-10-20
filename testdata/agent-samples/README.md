# Agentes de ejemplo

Dieciséis proyectos de agente "que ya existían": ninguno importa `rayito` y cada
uno trae su propio lockfile (Hermes y OpenCode son sólo configuración). Los usa
la aceptación de `agent-zero-config`: cada uno se despliega sin opciones y otra
vez con opciones propias. `manifest.json` lista lo que debe decidir la
detección de cada uno, y lo leen los tests de Python y de TypeScript.

Los de Python se bloquean con `uv lock` y los de TypeScript con
`npm install --package-lock-only`. El modelo y la región llegan por
`MODEL_ID` y `AWS_REGION`, como en cualquier agente de Bedrock.
