# syntax=docker/dockerfile:1
# Agente `strands-ts` exportado por Rayito. Construye con:
#   docker buildx build --platform linux/arm64 -t strands-ts .
FROM --platform=linux/arm64 python:3.12-slim-bookworm@sha256:34386ef0cb081344d7ec1c103ba398e6e9f64e9ab3a1509accc92a4e24a07258
RUN useradd --create-home --uid 1000 --shell /bin/bash user && mkdir -p /home/user/.rayito/agent /home/user/.rayito/agent/strands && chown -R user:user /home/user/.rayito
RUN apt-get update && apt-get install -y --no-install-recommends curl ca-certificates && rm -rf /var/lib/apt/lists/*
RUN curl -fsSL --retry 3 -o /tmp/node.tar.gz https://nodejs.org/dist/v22.23.3/node-v22.23.3-linux-arm64.tar.gz && echo '5ced2d48d1d7198739b7f86804de0171aefb6823b684b12341d3321afc3cb0b2  /tmp/node.tar.gz' | sha256sum -c - && mkdir -p /opt/agents/node && tar -xzf /tmp/node.tar.gz -C /opt/agents/node --strip-components=1 && rm -f /tmp/node.tar.gz && ln -sf /opt/agents/node/bin/node /usr/local/bin/node
COPY app/package.json /opt/agents/package.json
COPY app/package-lock.json /opt/agents/package-lock.json
RUN cd /opt/agents && PATH=/opt/agents/node/bin:$PATH npm ci --ignore-scripts --omit=dev --no-audit --no-fund
COPY node_bridge.mjs /opt/agents/rayito/node_bridge.mjs
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
CMD ["python3", "/opt/agents/rayito/agent_host", "serve", "/opt/agents/rayito/host.json", "--port", "8080"]
