"""``python3 -m rayito_kernel_sidecar --socket-root DIR --sidecar-root DIR
--default-cwd DIR --default-context-id ID``: serve ``rayd`` over stdio until
stdin closes or SIGTERM arrives."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import signal
import sys
from collections.abc import Mapping
from pathlib import Path

from rayito_kernel_sidecar.kernels import (
    ContextBase,
    KernelContext,
    KernelPaths,
    install_kernelspecs,
)
from rayito_kernel_sidecar.logging import SidecarLogger
from rayito_kernel_sidecar.server import ServerOptions, SidecarServer, StdioTransport


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="rayito_kernel_sidecar")
    parser.add_argument("--socket-root", default="/run/rayito/k")
    parser.add_argument("--sidecar-root", default="/opt/rayito/sidecar")
    parser.add_argument("--default-cwd", default="/home/user")
    parser.add_argument("--default-context-id", default="default")
    return parser.parse_args(argv)


async def serve(args: argparse.Namespace) -> int:
    logger = SidecarLogger()
    paths = KernelPaths(
        socket_root=Path(args.socket_root).resolve(), sidecar_root=Path(args.sidecar_root).resolve()
    )
    languages = tuple(install_kernelspecs(paths))
    transport = StdioTransport()
    await transport.open()
    server: SidecarServer | None = None

    def factory(context_id: str, language: str, cwd: str, envs: Mapping[str, str]) -> ContextBase:
        on_died = server.on_kernel_died if server is not None else None
        return KernelContext(context_id, language, cwd, envs, logger, paths, on_kernel_died=on_died)

    server = SidecarServer(
        transport,
        factory,
        logger,
        ServerOptions(
            default_context_id=args.default_context_id,
            default_cwd=args.default_cwd,
            languages=languages,
        ),
    )
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGTERM, signal.SIGINT):
        with contextlib.suppress(NotImplementedError, RuntimeError):
            loop.add_signal_handler(signum, server.stop)
    logger.info("sidecar starting", languages=list(languages))
    await server.run()
    logger.info("sidecar stopped")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    return asyncio.run(serve(args))


if __name__ == "__main__":
    sys.exit(main())
