"""`rayito._size_catalog.ConventionCatalog`: cachea `minimumMemoryInMiB` por
`(image_arn, image_version)` y por proceso, nunca llama dos veces para la
misma clave."""

from __future__ import annotations

from dataclasses import dataclass, field

from rayito._models import ImageVersionInfo
from rayito._size_catalog import ConventionCatalog

ARN = "arn:aws:lambda:us-east-1:123456789012:microvm-image:rayito-base-4gb"


@dataclass
class FakeImageVersionReader:
    minimum_memory_mib: int = 4096
    calls: list[tuple[str, str]] = field(default_factory=list)

    def get_microvm_image_version(self, image_arn: str, image_version: str) -> ImageVersionInfo:
        self.calls.append((image_arn, image_version))
        return ImageVersionInfo(minimum_memory_mib=self.minimum_memory_mib)


def test_first_call_reads_through_to_the_reader() -> None:
    reader = FakeImageVersionReader()
    catalog = ConventionCatalog()
    assert catalog.minimum_memory_mib(reader, ARN, "3") == 4096
    assert reader.calls == [(ARN, "3")]


def test_second_call_for_the_same_key_is_served_from_cache() -> None:
    reader = FakeImageVersionReader()
    catalog = ConventionCatalog()
    catalog.minimum_memory_mib(reader, ARN, "3")
    catalog.minimum_memory_mib(reader, ARN, "3")
    catalog.minimum_memory_mib(reader, ARN, "3")
    assert reader.calls == [(ARN, "3")]


def test_a_different_version_of_the_same_arn_is_a_different_cache_key() -> None:
    reader = FakeImageVersionReader()
    catalog = ConventionCatalog()
    catalog.minimum_memory_mib(reader, ARN, "3")
    catalog.minimum_memory_mib(reader, ARN, "4")
    assert reader.calls == [(ARN, "3"), (ARN, "4")]


def test_two_catalog_instances_do_not_share_a_cache() -> None:
    """Sólo `DEFAULT_SIZE_CATALOG` es la instancia compartida por proceso;
    una `ConventionCatalog()` nueva empieza vacía."""
    reader = FakeImageVersionReader()
    ConventionCatalog().minimum_memory_mib(reader, ARN, "3")
    assert ConventionCatalog().minimum_memory_mib(reader, ARN, "3") == 4096
    assert reader.calls == [(ARN, "3"), (ARN, "3")]
