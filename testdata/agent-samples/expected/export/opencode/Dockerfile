# syntax=docker/dockerfile:1
# Agente `opencode` exportado por Rayito. Construye con:
#   docker buildx build --platform linux/arm64 -t opencode .
FROM --platform=linux/arm64 python:3.12-slim-bookworm@sha256:34386ef0cb081344d7ec1c103ba398e6e9f64e9ab3a1509accc92a4e24a07258
RUN useradd --create-home --uid 1000 --shell /bin/bash user && mkdir -p /home/user/.rayito/agent /home/user/.rayito/agent/opencode && chown -R user:user /home/user/.rayito
RUN apt-get update && apt-get install -y --no-install-recommends curl ca-certificates && rm -rf /var/lib/apt/lists/*
RUN curl -fsSL --retry 3 -o /tmp/opencode.tar.gz https://github.com/anomalyco/opencode/releases/download/v1.18.34/opencode-linux-arm64.tar.gz && echo 'bbdb3f00c2c51e42e315525233151309724226a8776da8e9145e3b0fa3d5310f  /tmp/opencode.tar.gz' | sha256sum -c - && curl -fsSL --retry 3 -o /tmp/ripgrep.tar.gz https://github.com/BurntSushi/ripgrep/releases/download/15.2.0/ripgrep-15.2.0-aarch64-unknown-linux-gnu.tar.gz && echo 'a740b91c82eaf9914cfedd353572f2791cbe0162c84101ee0951058f4dcbc90d  /tmp/ripgrep.tar.gz' | sha256sum -c - && mkdir -p /opt/agents/bin && tar -xzf /tmp/opencode.tar.gz -C /opt/agents/bin opencode && tar -xzf /tmp/ripgrep.tar.gz -C /tmp && mv /tmp/ripgrep-15.2.0-aarch64-unknown-linux-gnu/rg /opt/agents/bin/rg && rm -rf /tmp/opencode.tar.gz /tmp/ripgrep.tar.gz /tmp/ripgrep-15.2.0-aarch64-unknown-linux-gnu && chown -R root:root /opt/agents && chmod 0755 /opt/agents /opt/agents/bin /opt/agents/bin/opencode /opt/agents/bin/rg && ln -sf /opt/agents/bin/opencode /usr/local/bin/opencode && ln -sf /opt/agents/bin/rg /usr/local/bin/rg
COPY agent_host /opt/agents/rayito/agent_host
COPY host.json /opt/agents/rayito/host.json
COPY app /opt/agents/app
RUN chown -R root:root /opt/agents && chmod -R a+rX,go-w /opt/agents
COPY --chown=user:user --chmod=0644 config/0 /home/user/.rayito/agent/opencode/opencode.json
ENV AWS_REGION="us-east-1"
ENV HOME="/home/user"
ENV OPENCODE_CONFIG="/home/user/.rayito/agent/opencode/opencode.json"
ENV OPENCODE_DISABLE_AUTOUPDATE="1"
ENV OPENCODE_DISABLE_CLAUDE_CODE="1"
ENV OPENCODE_DISABLE_DEFAULT_PLUGINS="1"
ENV OPENCODE_DISABLE_LSP_DOWNLOAD="1"
ENV OPENCODE_DISABLE_MODELS_FETCH="1"
ENV OPENCODE_EXPERIMENTAL_WEBSOCKETS="0"
ENV OPENCODE_PURE="1"
EXPOSE 8080
USER user
WORKDIR /home/user
HEALTHCHECK --interval=30s CMD python3 -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/ping')"
CMD ["python3", "/opt/agents/rayito/agent_host", "serve", "/opt/agents/rayito/host.json", "--port", "8080"]
