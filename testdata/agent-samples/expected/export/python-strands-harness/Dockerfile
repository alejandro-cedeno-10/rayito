# syntax=docker/dockerfile:1
# Agente `strands-harness` exportado por Rayito. Construye con:
#   docker buildx build --platform linux/arm64 -t strands-harness .
FROM --platform=linux/arm64 python:3.12-slim-bookworm@sha256:34386ef0cb081344d7ec1c103ba398e6e9f64e9ab3a1509accc92a4e24a07258
RUN useradd --create-home --uid 1000 --shell /bin/bash user && mkdir -p /home/user/.rayito/agent /home/user/.rayito/agent/strands && chown -R user:user /home/user/.rayito
COPY requirements-strands-harness.txt /opt/agents/requirements-strands-harness.txt
RUN echo '5a66609e6c9e66a537c4220f0f4b7a172e808bec72dda02998e933c3c512c634  /opt/agents/requirements-strands-harness.txt' | sha256sum -c - && python3 -m venv /opt/agents/strands-harness && /opt/agents/strands-harness/bin/python -m pip install --no-cache-dir --require-hashes --no-deps --only-binary=:all: -r /opt/agents/requirements-strands-harness.txt && /opt/agents/strands-harness/bin/python -m pip check
RUN apt-get update && apt-get install -y --no-install-recommends curl ca-certificates && rm -rf /var/lib/apt/lists/*
RUN curl -fsSL --retry 3 -o /tmp/uv.tar.gz https://github.com/astral-sh/uv/releases/download/0.13.1/uv-aarch64-unknown-linux-gnu.tar.gz && echo '9041c73c99fdc5892cf128140beb354eca9231d502068cc36e6cb53a8e59ad14  /tmp/uv.tar.gz' | sha256sum -c - && mkdir -p /opt/agents/uv && tar -xzf /tmp/uv.tar.gz -C /opt/agents/uv --strip-components=1 && rm -f /tmp/uv.tar.gz
COPY app/pyproject.toml /opt/agents/strands-harness-project/pyproject.toml
COPY app/uv.lock /opt/agents/strands-harness-project/uv.lock
RUN cd /opt/agents/strands-harness-project && UV_PROJECT_ENVIRONMENT=/opt/agents/strands-harness UV_PYTHON_DOWNLOADS=never /opt/agents/uv/uv sync --frozen --no-dev --no-install-project --inexact --python /opt/agents/strands-harness/bin/python
COPY agent_host /opt/agents/rayito/agent_host
COPY host.json /opt/agents/rayito/host.json
COPY app /opt/agents/app
RUN chown -R root:root /opt/agents && chmod -R a+rX,go-w /opt/agents
COPY --chown=user:user --chmod=0600 config/0 /home/user/.rayito/agent/strands/config.json
ENV AWS_REGION="us-east-1"
ENV OTEL_LOGS_EXPORTER="none"
ENV OTEL_METRICS_EXPORTER="none"
ENV OTEL_SDK_DISABLED="true"
ENV OTEL_TRACES_EXPORTER="none"
ENV PYTHONDONTWRITEBYTECODE="1"
ENV PYTHONUNBUFFERED="1"
EXPOSE 8080
USER user
WORKDIR /home/user
HEALTHCHECK --interval=30s CMD python3 -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/ping')"
CMD ["/opt/agents/strands-harness/bin/python", "/opt/agents/rayito/agent_host", "serve", "/opt/agents/rayito/host.json", "--port", "8080"]
