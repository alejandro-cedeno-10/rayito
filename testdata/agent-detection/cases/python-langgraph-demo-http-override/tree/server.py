from fastapi import FastAPI
from pydantic import BaseModel

from agent import build_graph

app = FastAPI()
graph = build_graph()


class Ask(BaseModel):
    message: str


@app.get("/health")
def health():
    return {"ok": True}


@app.post("/chat")
def chat(body: Ask):
    result = graph.invoke({"messages": [("user", body.message)]})
    return {"answer": result["messages"][-1].content}
