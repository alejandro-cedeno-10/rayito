"""Driver `strands-ts` del host: el nombre con el que 0.14.0-rc servía a
Strands Agents para TypeScript. Es el driver `node-callable` (el adaptador
`strands` es su valor por defecto); se mantiene como alias para que una
`host.json` ya escrita siga arrancando."""

from __future__ import annotations

from .node_callable import create

__all__ = ["create"]
