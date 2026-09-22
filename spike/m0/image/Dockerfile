# M0 probe image. Not the product image: it exists to measure the platform.
# Lambda builds this Dockerfile itself from the zip in S3, so no local Docker
# or ARM64 toolchain is required to run the spike.
FROM public.ecr.aws/lambda/microvms:al2023-minimal

RUN dnf install -y --setopt=install_weak_deps=0 \
      python3.12 python3.12-pip procps-ng iproute util-linux shadow-utils tar gzip \
    && (dnf install -y --setopt=install_weak_deps=0 iptables-nft || true) \
    && dnf clean all

RUN useradd -m -u 1000 -s /bin/bash user

RUN python3.12 -m pip install --no-cache-dir \
      jupyter_client==8.10.0 ipykernel==6.31.0

RUN mkdir -p /var/log/probe /opt/probe && chmod 0777 /var/log/probe

COPY probe.py /opt/probe/probe.py

ENV HOOK_PORT=9000 \
    APP_PORT=8080 \
    LOG_DIR=/var/log/probe \
    KERNEL_TRANSPORT=ipc \
    KERNEL_AUTOSTART=1

EXPOSE 8080 9000

CMD ["python3.12", "/opt/probe/probe.py"]
