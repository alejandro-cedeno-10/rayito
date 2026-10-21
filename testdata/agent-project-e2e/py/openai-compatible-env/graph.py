"""Grafo del e2e de «modelo elegido por tu código» (ADR-033 D2).

El único nodo no llama a ningún modelo: comprueba que el código que lee su
propia configuración (`LLM_MODEL`, `LLM_BASE_URL`, `LLM_API_KEY`) pasaría por
la pasarela de Rayito, y responde con booleanos y nombres, nunca con valores.
El nodo se llama como el del modelo (`model`) y contesta con un `AIMessage`:
el host toma el texto de la respuesta de los nodos del modelo.
"""

import json
import os

from langchain_core.messages import AIMessage
from langgraph.graph import END, START, MessagesState, StateGraph

#: El `upstream` del proyecto: si la URL que ve el código no lo nombra, pasa
#: por la pasarela de secretos de Rayito (MicroVM); en AgentCore es él mismo.
UPSTREAM_HOST = "llm-gateway.example.com"
#: El marcador de la clave que pone Rayito (`modelCredentialPlaceholder`).
PLACEHOLDER_PREFIX = "placeholder"
#: El nodo cuya respuesta es el texto de la invocación.
MODEL_NODE = "model"


def _checks() -> dict[str, object]:
    base_url = os.environ.get("LLM_BASE_URL", "")
    api_key = os.environ.get("LLM_API_KEY", "")
    return {
        "base_url_set": bool(base_url),
        "base_url_is_gateway": bool(base_url) and UPSTREAM_HOST not in base_url,
        "api_key_is_placeholder": api_key.lower().startswith(PLACEHOLDER_PREFIX),
        "model_env": "LLM_MODEL",
    }


def answer(state: MessagesState) -> dict[str, object]:
    return {"messages": [AIMessage(content=json.dumps(_checks()))]}


def build_graph(ctx: object = None) -> object:
    builder = StateGraph(MessagesState)
    builder.add_node(MODEL_NODE, answer)
    builder.add_edge(START, MODEL_NODE)
    builder.add_edge(MODEL_NODE, END)
    return builder.compile()
