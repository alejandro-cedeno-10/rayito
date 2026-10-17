"""API HTTP del asistente: `/health` y `/chat` sobre el grafo."""

from fastapi import FastAPI
from pydantic import BaseModel

from graph import build_graph

app = FastAPI()
graph = build_graph()


class Chat(BaseModel):
    message: str


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/chat")
def chat(body: Chat) -> dict[str, str]:
    result = graph.invoke({"messages": [("user", body.message)]})
    return {"answer": str(result["messages"][-1].content)}
