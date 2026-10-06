# Variante del guest local con dos agentes instalados, para el spike de
# docs/research/2026-10-agent-spike.md (fase 0; no es una imagen de
# producto ni se publica). Parte del guest de `make local-up`
# (rayito-local-guest:dev, la imagen de image/Dockerfile) y añade, todo de
# root y 0755 para que uid 1000 no pueda reemplazarlo:
#
#   - OpenCode (anomalyco/opencode, MIT): el binario único de la release
#     linux arm64 glibc, fijado por versión y sha256 (el `digest` que GitHub
#     publica para el asset, comprobado al descargarlo el 2026-10-05).
#   - ripgrep (MIT/Unlicense): OpenCode lo usa para sus herramientas de
#     búsqueda y, si no lo encuentra, intenta descargarlo, cosa que bajo
#     deny-all falla; por eso va preinstalado.
#   - deepagents 0.7.x (LangChain, MIT) en un venv propio con Python 3.12,
#     pines con hash en requirements-deepagents.txt.
#
# Las variables OPENCODE_* apagan todo lo que OpenCode intentaría bajar de
# Internet en el arranque (autoupdate, catálogo de models.dev, servidores
# LSP, plugins por defecto); con el egress cerrado fallarían igual, pero así
# no esperan a un timeout. Mismas reglas de pines que image/Dockerfile
# (scripts/check_pins.py: curl -o + sha256sum -c, pip con hashes).
ARG GUEST_IMAGE=rayito-local-guest:dev
FROM ${GUEST_IMAGE}

RUN OPENCODE_VERSION=1.18.34 \
    && OPENCODE_SHA256=bbdb3f00c2c51e42e315525233151309724226a8776da8e9145e3b0fa3d5310f \
    && RIPGREP_VERSION=15.2.0 \
    && RIPGREP_SHA256=a740b91c82eaf9914cfedd353572f2791cbe0162c84101ee0951058f4dcbc90d \
    && curl -fsSL --retry 3 -o /tmp/opencode.tar.gz \
         "https://github.com/anomalyco/opencode/releases/download/v${OPENCODE_VERSION}/opencode-linux-arm64.tar.gz" \
    && echo "${OPENCODE_SHA256}  /tmp/opencode.tar.gz" | sha256sum -c - \
    && curl -fsSL --retry 3 -o /tmp/ripgrep.tar.gz \
         "https://github.com/BurntSushi/ripgrep/releases/download/${RIPGREP_VERSION}/ripgrep-${RIPGREP_VERSION}-aarch64-unknown-linux-gnu.tar.gz" \
    && echo "${RIPGREP_SHA256}  /tmp/ripgrep.tar.gz" | sha256sum -c - \
    && mkdir -p /opt/agents/bin \
    && tar -xzf /tmp/opencode.tar.gz -C /opt/agents/bin opencode \
    && tar -xzf /tmp/ripgrep.tar.gz -C /tmp \
    && mv "/tmp/ripgrep-${RIPGREP_VERSION}-aarch64-unknown-linux-gnu/rg" /opt/agents/bin/rg \
    && rm -rf /tmp/opencode.tar.gz /tmp/ripgrep.tar.gz "/tmp/ripgrep-${RIPGREP_VERSION}-aarch64-unknown-linux-gnu" \
    && chown -R root:root /opt/agents \
    && chmod 0755 /opt/agents /opt/agents/bin /opt/agents/bin/opencode /opt/agents/bin/rg \
    && ln -sf /opt/agents/bin/opencode /usr/local/bin/opencode \
    && ln -sf /opt/agents/bin/rg /usr/local/bin/rg

COPY requirements-deepagents.txt /opt/agents/requirements-deepagents.txt
RUN python3 -m venv /opt/agents/deepagents \
    && /opt/agents/deepagents/bin/python -m pip install --no-cache-dir \
         --require-hashes --no-deps --only-binary=:all: \
         -r /opt/agents/requirements-deepagents.txt \
    && /opt/agents/deepagents/bin/python -m pip check \
    && chown -R root:root /opt/agents \
    && chmod -R a+rX,go-w /opt/agents

ENV OPENCODE_DISABLE_AUTOUPDATE=1 \
    OPENCODE_DISABLE_MODELS_FETCH=1 \
    OPENCODE_DISABLE_LSP_DOWNLOAD=1 \
    OPENCODE_DISABLE_DEFAULT_PLUGINS=1 \
    OPENCODE_PURE=1

RUN su user -c "opencode --version" \
    && su user -c "rg --version" \
    && su user -c "/opt/agents/deepagents/bin/python -c 'import deepagents, langchain_aws'"
