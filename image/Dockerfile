# Imagen de producto, hito M6: al2023-minimal + Python 3.12 + rayd precompilado
# + kernel-sidecar con el stack científico pineado. Lambda construye este
# Dockerfile en su propia VM Graviton a partir del zip (scripts/image_zip.py):
# nunca se compila Rust aquí y los pines llegan como wheels manylinux aarch64
# (sin compiladores). Tras arrancar CMD, Lambda espera el 200 de /ready (el
# kernel por defecto ya calentado por el sidecar) y toma el snapshot con los
# procesos vivos; /validate corre una celda real de pandas + matplotlib en una
# VM desechable (ARCHITECTURE.md, Capa 1). M5 añadió PtyService, Code.Reattach
# y los hooks /suspend y /resume reales. M6 (endurecimiento) añade iproute:
# la misma imagen publicada con additionalOsCapabilities ALL
# (`make image-publish-caps`, nombre rayito-base-caps) deja que rayd instale
# en el arranque la ruta de política que bloquea IMDS para uid 1000-65535
# (`ip rule add uidrange 1000-65535 lookup 100` + `ip route replace
# blackhole 169.254.169.254/32 table 100`; el kernel del guest no trae el
# match `owner` de x_tables y el agente de la plataforma usa uids 991-994
# con su propio canal a IMDS, medido en AWS_API_NOTES.md Q48); en la imagen
# por defecto (sin CAP_NET_ADMIN) rayd lo registra y sigue (fail-open). M7
# añade la variante poly (`make image-publish-poly`, nombre rayito-base-poly):
# el kernel bash instalado por una capa condicional que sólo corre con el
# marcador kernels_variant del zip. Un solo Dockerfile, cuatro nombres de
# imagen (rayito-base, -slim, -caps, -poly).
#
# rayd corre como root (uid 0 con capabilities capadas) porque debe hacer
# setuid al usuario `user` (uid 1000) en cada proceso, en el sidecar y en
# setfsuid/setfsgid en cada operación de fichero; la imagen NO define
# RAYITO_ALLOW_ROOT, así que `user="root"` se rechaza con PERMISSION_DENIED.
# El sidecar y los kernels corren como `user`; sus sockets ipc viven en
# /run/rayito/k, que rayd prepara (0700, uid 1000) en cada arranque.
#
# Imagen base fijada por digest (SECURITY.md T10). El tag se conserva delante
# del `@sha256:` por legibilidad; lo que se resuelve es el digest de la
# manifest list, que es lo que `docker buildx imagetools inspect`, `docker
# manifest inspect` y el ecosistema `docker` de Dependabot devuelven para el
# tag, así que los bumps son mecánicos. Registrado 2026-09-16:
#   manifest list  sha256:05cb9b38d841e7ff1b693dc9e894909612f340bf99ec97d426e8000a5bbe96c3
#   linux/arm64    sha256:63831a97f9e498f7693c6f42951fe5d935947e6ae25a11fcab1fe0f5ae60b2a7
#   (el único manifest que contiene: la imagen que realmente se descarga)
# Cómo refrescarlo cuando AWS mueva el tag (un bump de digest es una versión
# de imagen nueva: se publica y se acepta con el e2e como cualquier otra):
#   con Docker:  docker buildx imagetools inspect public.ecr.aws/lambda/microvms:al2023-minimal
#   sin Docker:  TOKEN=$(curl -fsSL "https://public.ecr.aws/token/?scope=repository:lambda/microvms:pull" | jq -r .token)
#                curl -fsSI -H "Authorization: Bearer $TOKEN" \
#                  -H "Accept: application/vnd.docker.distribution.manifest.list.v2+json" \
#                  https://public.ecr.aws/v2/lambda/microvms/manifests/al2023-minimal | grep -i docker-content-digest
# La versión de la imagen base gestionada (`baseImageVersion`) va aparte, en
# cada publish: `--base-image-version` obligatoria en scripts/publish_image.py
# (AWS_API_NOTES.md §4 y Q52).
FROM public.ecr.aws/lambda/microvms:al2023-minimal@sha256:05cb9b38d841e7ff1b693dc9e894909612f340bf99ec97d426e8000a5bbe96c3

RUN dnf install -y --setopt=install_weak_deps=0 \
      python3.12 python3.12-pip tar gzip procps-ng shadow-utils util-linux \
      iproute ca-certificates \
    && dnf clean all \
    && useradd -m -u 1000 -s /bin/bash user \
    && ln -sf /usr/bin/python3.12 /usr/local/bin/python3 \
    && ln -sf /usr/bin/python3.12 /usr/bin/python3

# Los tests de aceptación de M2-M6 dependen de estas herramientas en el PATH
# del hijo (/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin): la
# build falla aquí, no en AWS, si falta alguna. `tty` y `stty` los usa la PTY
# de M5; `ip` (iproute) y `/usr/bin/python3` (la sonda de IMDS como uid 1000
# se lanza por ruta absoluta, sin shell) los usa rayd para el bloqueo de IMDS
# de M6 (el producto no depende del contenido incidental de la base);
# `test -c /dev/ptmx` es informativo en build (el guest real se mide en el
# e2e de M5, bloque 1). El bundle de CAs (`ca-certificates`) es lo que el
# cliente S3 de rayd (rustls + rustls-native-certs, ADR-009) carga para
# verificar TLS: sin él Checkpoint/Restore fallan en la primera conexión.
RUN for tool in /bin/bash whoami id sleep head tr env python3 /usr/bin/python3 ps \
                sha256sum stat rm mv mkdir chmod touch ln tty stty ip; do \
      command -v "$tool" >/dev/null || { echo "missing $tool"; exit 1; }; \
    done \
    && test -f /etc/pki/tls/certs/ca-bundle.crt \
    && python3 -c "import sys; assert sys.version_info[:2] == (3, 12), sys.version" \
    && { test -c /dev/ptmx && echo "pty devices: /dev/ptmx present at build" \
         || echo "pty devices: /dev/ptmx not visible at build (informative)"; }

# Sidecar + pines exactos (kernel-sidecar/requirements.txt) en el site-packages
# del sistema, legibles por `user`; `pip check` falla la build ante cualquier
# conflicto de versiones en vez de dejarlo para el arranque.
COPY kernel-sidecar/ /opt/rayito/sidecar/
RUN chmod -R a+rX /opt/rayito/sidecar \
    && python3 -m pip install --no-cache-dir --break-system-packages \
         -r /opt/rayito/sidecar/requirements.txt \
    && python3 -m pip check

# Variante poly (M7, `make image-publish-poly` → rayito-base-poly): el marcador
# kernel-sidecar/kernels_variant sólo existe dentro del zip que escribe
# `scripts/image_zip.py --variant poly` (la API de imágenes no tiene build
# args), así que esta capa es un no-op en rayito-base, que no lleva el
# marcador y no ejecuta nada de esto (su snapshot no cambia). Con el marcador
# instala bash_kernel con los pines de requirements-poly.txt. Sólo bash:
# ijavascript 5.2.1 → jmp 2 → zeromq 5.3.1 no publica prebuilds para
# linux-arm64 ni para Node 20 (ABI 115) y su `node-gyp rebuild` muere con
# `not found: make` en al2023-minimal, que no trae compiladores por diseño
# (AWS_API_NOTES.md Q57, medido 2026-09-16); no se añaden compiladores a la
# imagen. El kernel bash NO se calienta antes de /ready: el sidecar lo
# arranca en la primera celda de ese lenguaje (docs/site/docs/kernels.md).
RUN if [ "$(cat /opt/rayito/sidecar/kernels_variant 2>/dev/null)" = "poly" ]; then \
      python3 -m pip install --no-cache-dir --break-system-packages \
           -r /opt/rayito/sidecar/requirements-poly.txt \
      && python3 -m pip check \
      && su user -c "python3 -c 'import bash_kernel'" \
      && test -f /opt/rayito/sidecar/jupyter/kernels/rayito-bash/kernel.json; \
    fi

# Calentamiento en tiempo de build como `user`: la caché de fuentes de
# matplotlib queda en el home dentro del snapshot, así el primer kernel no la
# construye (segundos) en cada MicroVM; el perfil de IPython lo crea el kernel
# por defecto antes de /ready, también dentro del snapshot.
RUN su user -c "MPLCONFIGDIR=/home/user/.cache/matplotlib python3 -c 'import matplotlib.pyplot, pandas, numpy, scipy, sklearn, ipykernel, jupyter_client, zmq; import matplotlib.font_manager as fm; fm.fontManager'" \
    && test -n "$(ls /home/user/.cache/matplotlib/fontlist-*.json)" \
    && su user -c "PYTHONPATH=/opt/rayito/sidecar/src python3 -c 'import rayito_kernel_sidecar, rayito_kernel_sidecar._vendor.e2b_charts'" \
    && python3 -m ipykernel --version \
    && test -f /opt/rayito/sidecar/jupyter/kernels/rayito/kernel.json \
    && test -f /opt/rayito/sidecar/ipython/ipython_kernel_config.py

COPY rayd /usr/local/bin/rayd
RUN chmod 0755 /usr/local/bin/rayd

EXPOSE 8080 9000

CMD ["/usr/local/bin/rayd", "--grpc-port", "8080", "--hooks-port", "9000"]
