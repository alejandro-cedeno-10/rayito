# syntax=docker/dockerfile:1
# Agente `hermes` exportado por Rayito. Construye con:
#   docker buildx build --platform linux/arm64 -t hermes .
FROM --platform=linux/arm64 python:3.12-slim-bookworm@sha256:34386ef0cb081344d7ec1c103ba398e6e9f64e9ab3a1509accc92a4e24a07258
RUN useradd --create-home --uid 1000 --shell /bin/bash user && mkdir -p /home/user/.rayito/agent /home/user/.rayito/agent/hermes && chown -R user:user /home/user/.rayito
RUN apt-get update && apt-get install -y --no-install-recommends curl ca-certificates git && rm -rf /var/lib/apt/lists/*
RUN mkdir -p /opt/agents/hermes && curl -fsSL --retry 3 -o /tmp/hermes-python.tar.gz https://github.com/astral-sh/python-build-standalone/releases/download/20261013/cpython-3.14.8%2B20261013-aarch64-unknown-linux-gnu-install_only.tar.gz && echo '6c3cfb175acca2748d5a9e42208c523bfbc1099ddb928615b5f328ba7c106e83  /tmp/hermes-python.tar.gz' | sha256sum -c - && tar -xzf /tmp/hermes-python.tar.gz -C /opt/agents/hermes && rm -f /tmp/hermes-python.tar.gz
COPY requirements-hermes.txt /opt/agents/requirements-hermes.txt
RUN echo 'f9a5b57e84bb8f953ca279b33757efb3443551548ef605e5ce6e9bbda4dff776  /opt/agents/requirements-hermes.txt' | sha256sum -c - && /opt/agents/hermes/python/bin/python3 -m venv /opt/agents/hermes/venv && /opt/agents/hermes/venv/bin/python -m pip install --no-cache-dir --require-hashes --no-deps --only-binary=:all: -r /opt/agents/requirements-hermes.txt && /opt/agents/hermes/venv/bin/python -m pip check
RUN git clone --depth 1 --branch v0.22.2 https://github.com/NousResearch/hermes-agent /opt/agents/hermes/src && test "$(git -C /opt/agents/hermes/src rev-parse HEAD)" = d923a5eb487a17c0a68801bba9ef0f56c81040a8 && cd /opt/agents/hermes/src && rm -rf .git tests tests-js apps website ui-tui web contributors evals docker nix
COPY agent_host /opt/agents/rayito/agent_host
COPY host.json /opt/agents/rayito/host.json
COPY app /opt/agents/app
RUN chown -R root:root /opt/agents && chmod -R a+rX,go-w /opt/agents
COPY --chown=user:user --chmod=0600 config/0 /home/user/.rayito/agent/hermes/config.json
ENV API_SERVER_PORT="8642"
ENV AWS_REGION="us-east-1"
ENV HERMES_HOME="/home/user/.rayito/agent/hermes/home"
ENV PYTHONDONTWRITEBYTECODE="1"
ENV PYTHONUNBUFFERED="1"
EXPOSE 8080
USER user
WORKDIR /home/user
HEALTHCHECK --interval=30s CMD python3 -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/ping')"
CMD ["/opt/agents/hermes/venv/bin/python", "/opt/agents/rayito/agent_host", "serve", "/opt/agents/rayito/host.json", "--port", "8080"]
