# syntax=docker/dockerfile:1
# Agente `flask-custom` exportado por Rayito. Construye con:
#   docker buildx build --platform linux/arm64 -t flask-custom .
FROM --platform=linux/arm64 python:3.12-slim-bookworm@sha256:34386ef0cb081344d7ec1c103ba398e6e9f64e9ab3a1509accc92a4e24a07258
RUN useradd --create-home --uid 1000 --shell /bin/bash user && mkdir -p /home/user/.rayito/agent  && chown -R user:user /home/user/.rayito
RUN apt-get update && apt-get install -y --no-install-recommends curl ca-certificates && rm -rf /var/lib/apt/lists/*
RUN curl -fsSL --retry 3 -o /tmp/uv.tar.gz https://github.com/astral-sh/uv/releases/download/0.13.1/uv-aarch64-unknown-linux-gnu.tar.gz && echo '9041c73c99fdc5892cf128140beb354eca9231d502068cc36e6cb53a8e59ad14  /tmp/uv.tar.gz' | sha256sum -c - && mkdir -p /opt/agents/uv && tar -xzf /tmp/uv.tar.gz -C /opt/agents/uv --strip-components=1 && rm -f /tmp/uv.tar.gz
RUN /opt/agents/uv/uv venv --python python3 /opt/agents/custom
COPY app/pyproject.toml /opt/agents/custom-project/pyproject.toml
COPY app/uv.lock /opt/agents/custom-project/uv.lock
RUN cd /opt/agents/custom-project && UV_PROJECT_ENVIRONMENT=/opt/agents/custom UV_PYTHON_DOWNLOADS=never /opt/agents/uv/uv sync --frozen --no-dev --no-install-project --python python3
COPY agent_host /opt/agents/rayito/agent_host
COPY host.json /opt/agents/rayito/host.json
COPY app /opt/agents/app
RUN chown -R root:root /opt/agents && chmod -R a+rX,go-w /opt/agents
ENV AWS_REGION="us-east-1"
ENV PYTHONDONTWRITEBYTECODE="1"
ENV PYTHONUNBUFFERED="1"
ENV RAYITO_MODEL_ID="us.anthropic.claude-haiku-4-5-20251001-v1:0"
ENV RAYITO_MODEL_PROVIDER="bedrock"
ENV RAYITO_MODEL_REGION="us-east-1"
EXPOSE 8080
USER user
WORKDIR /home/user
HEALTHCHECK --interval=30s CMD python3 -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/ping')"
CMD ["/opt/agents/custom/bin/python", "/opt/agents/rayito/agent_host", "serve", "/opt/agents/rayito/host.json", "--port", "8080"]
