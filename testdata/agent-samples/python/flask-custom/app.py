"""Servicio HTTP propio: `/healthz` y `/chat`, con un modelo de Bedrock por Converse."""

import os

import boto3
from flask import Flask, jsonify, request

app = Flask(__name__)
client = boto3.client("bedrock-runtime", region_name=os.environ.get("AWS_REGION"))
SYSTEM = [{"text": "You answer frequently asked questions in one short sentence."}]


@app.get("/healthz")
def healthz():
    return jsonify(status="ok")


@app.post("/chat")
def chat():
    prompt = request.get_json(force=True)["prompt"]
    reply = client.converse(
        modelId=os.environ["MODEL_ID"],
        system=SYSTEM,
        messages=[{"role": "user", "content": [{"text": prompt}]}],
    )
    return jsonify(text=reply["output"]["message"]["content"][0]["text"])
