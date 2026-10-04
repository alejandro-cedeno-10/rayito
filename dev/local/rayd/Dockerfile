# Compila `rayd` para el guest local (docs/site/docs/guias/probar-en-local.md)
# sin toolchain de Rust en el host: `make local-guest-context` construye esta
# imagen, copia `/rayd` fuera con `docker create` + `docker cp` y la deja al
# lado del Dockerfile de producto (image/Dockerfile), que es el que de verdad
# construye el guest. Con `RAYD_BIN=<ruta>` (p. ej. el binario de `make
# build`) este paso se salta.
#
# Se compila sobre la misma base que la imagen de producto, fijada por el
# mismo digest que image/Dockerfile, para que el binario glibc enlace contra
# la glibc 2.34 con la que va a correr (una toolchain de Debian enlazaría
# símbolos de una glibc más nueva). Solo arm64: la base no publica otra
# arquitectura, igual que en AWS (CLAUDE.md, regla 5). Sin sintaxis de
# BuildKit: construye también con el builder clásico.
FROM public.ecr.aws/lambda/microvms:al2023-minimal@sha256:a2095a5ece1ce7b9c31f561d548da06dc144cabca30f1cfe40823a832a30114f

# aws-lc-sys (rustls) compila C con cmake; ring, con cc.
RUN dnf install -y --setopt=install_weak_deps=0 \
      gcc gcc-c++ make cmake perl tar gzip ca-certificates \
    && dnf clean all

# rustup-init clavado por versión y sha256 (el `.sha256` publicado junto al
# binario en static.rust-lang.org, consultado 2026-10-04). El toolchain sale de
# rust-toolchain.toml: rustup lo instala, con sus componentes y targets, la
# primera vez que corre `cargo` dentro del árbol.
ENV RUSTUP_HOME=/opt/rustup CARGO_HOME=/opt/cargo PATH=/opt/cargo/bin:$PATH
RUN RUSTUP_VERSION=1.29.1 \
    && RUSTUP_INIT_SHA256=15f6e4ce9f583b929c996c91562bad6d4454f3281de858b02cdfdef615fac433 \
    && curl -fsSL --retry 3 -o /tmp/rustup-init \
         "https://static.rust-lang.org/rustup/archive/${RUSTUP_VERSION}/aarch64-unknown-linux-gnu/rustup-init" \
    && echo "${RUSTUP_INIT_SHA256}  /tmp/rustup-init" | sha256sum -c - \
    && chmod 0755 /tmp/rustup-init \
    && /tmp/rustup-init -y --no-modify-path --profile minimal --default-toolchain none \
    && rm -f /tmp/rustup-init

WORKDIR /build
COPY rust-toolchain.toml Cargo.toml Cargo.lock ./
COPY proto proto
COPY crates crates

# `CARGO_JOBS` acota el paralelismo: el enlazado de rayd necesita ~1 GiB por
# job. Compilar aws-sdk-s3 pasa de 2 GiB aunque sea con un solo job, así que
# esta imagen pide una VM de Docker de unos 6 GiB; con menos, `LOCAL_RAYD_BIN`
# (docs/site/docs/guias/probar-en-local.md).
ARG CARGO_JOBS=2
RUN cargo build --release --locked -p rayd -j "${CARGO_JOBS}" \
    && cp target/release/rayd /rayd \
    && rm -rf target \
    && test -x /rayd
