"""`BaseImagePublisher` de `rayito setup` sobre el pipeline de `rayito image
publish` (`_publish.build_or_reuse`): subida del zip por contenido, `create`
o `update-microvm-image`, reuso de una versión idéntica y puerta de tres
estados. Vive aquí, y no en `_account_setup`, porque ese pipeline vive en
`cli/_publish.py`. Sin `typer`."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Final

from rayito._account_setup._domain import (
    BASE_IMAGE_VARIANT,
    AccountSetupException,
    PublishedImage,
)
from rayito._account_setup._ports import Progress
from rayito._images import DEFAULT_BUILD_TIMEOUT_SECONDS, DEFAULT_MEMORY_MIB, Sleeper
from rayito.cli._publish import (
    DEFAULT_STACK_NAME,
    LATEST_BASE_IMAGE_VERSION,
    PublishClients,
    PublishSettings,
    build_or_reuse,
    resolve_base_image_version,
)

#: La línea que anuncia la versión de la base gestionada elegida, igual que
#: `rayito image publish --base-image-version latest`.
BASE_VERSION_LINE_TEMPLATE: Final = "base_image_version={version}"


class PipelineImagePublisher:
    """Publica `rayito-base` (variante `full`, sin capacidades extra) o
    `rayito-base-caps` (el mismo zip con `additionalOsCapabilities=["ALL"]`,
    como `rayito image publish --os-capabilities ALL`) sobre la versión más
    reciente de la base gestionada. Construirlo no llama a AWS."""

    def __init__(self, clients: PublishClients, *, sleep: Sleeper = time.sleep) -> None:
        self._clients = clients
        self._sleep = sleep

    def publish(
        self,
        artifact: Path,
        *,
        name: str,
        os_capabilities: str | None,
        bucket: str,
        build_role_arn: str,
        on_progress: Progress,
    ) -> PublishedImage:
        version = resolve_base_image_version(
            self._clients.microvms, self._clients.region, LATEST_BASE_IMAGE_VERSION
        )
        on_progress(BASE_VERSION_LINE_TEMPLATE.format(version=version))
        settings = PublishSettings(
            artifact=artifact,
            image_name=name,
            variant=BASE_IMAGE_VARIANT,
            bucket=bucket,
            stack_name=DEFAULT_STACK_NAME,
            build_role_arn=build_role_arn,
            base_image_version=version,
            memory_mib=DEFAULT_MEMORY_MIB,
            force=False,
            timeout_seconds=DEFAULT_BUILD_TIMEOUT_SECONDS,
            os_capabilities=os_capabilities,
        )
        summary = build_or_reuse(self._clients, settings, sleep=self._sleep, emit=on_progress)
        if not summary["launchable"]:
            raise AccountSetupException(
                "image_build_failed",
                name=name,
                imageVersion=str(summary["imageVersion"]),
                state=str(summary["gate"]["versionState"]),
            )
        return PublishedImage(
            image_arn=str(summary["imageArn"]),
            image_version=str(summary["imageVersion"]),
            artifact_uploaded=bool(summary["artifactUploaded"]),
        )
