"""Kernel sidecar of ``rayd``: one Jupyter kernel per context, JSON lines over
stdio (ADR-002). Spawned by ``rayd`` as ``python3 -m rayito_kernel_sidecar``;
never published to PyPI."""

from rayito_kernel_sidecar.protocol import PROTOCOL_VERSION

__all__ = ["PROTOCOL_VERSION"]
__version__ = "0.0.4"
