from fastapi import FastAPI

app = FastAPI()


@app.get("/health")
def health():
    return {"ok": True}


@app.post("/ask")
def ask(question: str):
    return {"answer": question}
