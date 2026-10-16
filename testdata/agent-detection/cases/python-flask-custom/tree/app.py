from flask import Flask, jsonify, request

app = Flask(__name__)


@app.route("/healthz")
def healthz():
    return jsonify(status="ok")


@app.route("/chat", methods=["POST"])
def chat():
    body = request.get_json()
    return jsonify({"output": "eco: " + body["message"]})
