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
# marcador kernels_variant del zip; M9 suma en esa misma capa el binario de
# Deno fijado por sha256 para los kernels javascript y typescript. Un solo
# Dockerfile, cuatro nombres de imagen (rayito-base, -slim, -caps, -poly).
#
# rayd corre como root (uid 0 con capabilities capadas) porque debe hacer
# setuid al usuario `user` (uid 1000) en cada proceso, en el sidecar y en
# setfsuid/setfsgid en cada operación de fichero; la imagen NO define
# RAYITO_ALLOW_ROOT, así que `user="root"` se rechaza con PERMISSION_DENIED.
# El sidecar y los kernels corren como `user`; los sockets ipc de los kernels
# Python y bash viven en /run/rayito/k, que rayd prepara (0700, uid 1000) en
# cada arranque. La excepción son los kernels Deno de rayito-base-poly: Deno
# sólo sabe escuchar en TCP, así que usan 127.0.0.1 con la clave HMAC en el
# fichero de conexión, que sigue en /run/rayito/k (ARCHITECTURE.md, ADR-013).
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

# git-core (M9) es lo que ejecuta el módulo git de los SDK (`sandbox.git`, un
# envoltorio de `commands.run` en el cliente). Va fijado por versión y release
# (el gate dnf de scripts/check_pins.py lo exige) y en esta misma capa, así
# que las cuatro variantes traen `git`. Medido en una build arm64 emulada de
# esta base: 7 paquetes (git-core, less, libcbor, libedit, libfido2, openssh,
# openssh-clients), 31,452,146 B de tamaño instalado según RPM. No se calienta
# nada, así que el snapshot de memoria no cambia. openssh-clients no abre
# superficie de entrada: no trae demonio ni puerto (SECURITY.md T10). Subir
# git es una versión de imagen nueva: cambiar la NEVRA, republicar y repetir
# el e2e.
RUN dnf install -y --setopt=install_weak_deps=0 \
      python3.12 python3.12-pip tar gzip procps-ng shadow-utils util-linux \
      iproute ca-certificates git-core-2.50.1-1.amzn2023.0.1 \
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
# `git` lo usa el módulo git de M9: si falta, falla la build y no el e2e.
RUN for tool in /bin/bash whoami id sleep head tr env python3 /usr/bin/python3 ps \
                sha256sum stat rm mv mkdir chmod touch ln tty stty ip git; do \
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
# marcador y no ejecuta nada de esto (su snapshot no cambia, no existe
# /opt/rayito/deno y el sidecar no publica los kernelspecs de Deno). Con el
# marcador:
#   - instala bash_kernel con los pines de requirements-poly.txt;
#   - descarga Deno 2.9.7 (aarch64-unknown-linux-gnu, un único binario sin
#     npm, node-gyp ni zeromq, que corre sobre la glibc 2.34 de la imagen,
#     AWS_API_NOTES.md Q61), fijado por versión exacta y sha256, verificado
#     con `sha256sum -c` (SECURITY.md T10; scripts/check_pins.py exige a
#     cada curl de este fichero escribir con -o una ruta que compruebe un
#     `sha256sum -c` de la misma instrucción, y rechaza `curl | sh`, wget y
#     ADD con URL). ijavascript quedó descartado:
#     su zeromq 5.3.1 no tiene prebuild linux-arm64 y la imagen no trae
#     compiladores por diseño (Q57);
#   - lo extrae con `zipfile` de Python porque la imagen no trae `unzip`
#     (zipfile pierde el bit de ejecución, de ahí el chmod) y lo deja en
#     /opt/rayito/deno/deno, de root y 0755, para que uid 1000 no pueda
#     reemplazarlo (/opt/rayito ya está en la lista de denegación de
#     FilesystemService, T11);
#   - comprueba `deno --version` como `user`, así un problema de glibc o de
#     permisos rompe la build y no la primera celda.
# Ni bash ni Deno se calientan antes de /ready: el sidecar arranca cada
# kernel en la primera celda o contexto de su lenguaje
# (docs/site/docs/kernels.md). Subir Deno es una versión de imagen nueva:
# cambiar DENO_VERSION y DENO_SHA256 (el asset
# deno-aarch64-unknown-linux-gnu.zip de la release y su .sha256sum oficial),
# republicar rayito-base-poly y repetir su e2e; Dependabot no lo sigue.
RUN if [ "$(cat /opt/rayito/sidecar/kernels_variant 2>/dev/null)" = "poly" ]; then \
      python3 -m pip install --no-cache-dir --break-system-packages \
           -r /opt/rayito/sidecar/requirements-poly.txt \
      && python3 -m pip check \
      && su user -c "python3 -c 'import bash_kernel'" \
      && test -f /opt/rayito/sidecar/jupyter/kernels/rayito-bash/kernel.json \
      && DENO_VERSION=2.9.7 \
      && DENO_SHA256=c832298b1ad4422481334855f6003e0f54145762c5a134f20a489511d2f65bbf \
      && curl -fsSL --retry 3 -o /tmp/deno.zip \
           "https://github.com/denoland/deno/releases/download/v${DENO_VERSION}/deno-aarch64-unknown-linux-gnu.zip" \
      && echo "${DENO_SHA256}  /tmp/deno.zip" | sha256sum -c - \
      && mkdir -p /opt/rayito/deno \
      && python3 -c "import zipfile; zipfile.ZipFile('/tmp/deno.zip').extract('deno', '/opt/rayito/deno')" \
      && rm -f /tmp/deno.zip \
      && chown -R root:root /opt/rayito/deno \
      && chmod 0755 /opt/rayito/deno /opt/rayito/deno/deno \
      && su user -c "DENO_NO_UPDATE_CHECK=1 NO_COLOR=1 /opt/rayito/deno/deno --version" | grep -q "^deno ${DENO_VERSION} " \
      && test -f /opt/rayito/sidecar/jupyter/kernels/rayito-javascript/kernel.json \
      && test -f /opt/rayito/sidecar/jupyter/kernels/rayito-typescript/kernel.json; \
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
