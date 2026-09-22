"""Verify the dependency list that ``cargo auditable`` embeds in ``rayd``.

``cargo auditable`` stores the exact crate graph of a binary as zlib-compressed
JSON in the ELF section ``.dep-v0`` (https://github.com/rust-secure-code/cargo-auditable,
format ``audit-info``: ``{"packages": [{"name", "version", "source", "kind",
"dependencies", "root"}, ...]}``). This script reads that section with the
standard library only (no ``cargo-audit`` on the developer machine) and
asserts what ``make build``, the CI ``build`` job and the release job rely on:

- the section exists (a plain ``cargo zigbuild`` binary has none);
- the root package is ``rayd`` at the version of the root ``Cargo.toml``
  (``[workspace.package].version``, the value release-please bumps);
- ``tonic``, ``tokio``, ``axum`` and ``nix`` are in the graph (the crates the
  agent cannot run without, so a truncated list is caught).

On success it prints the package count and the root; on any failure it prints
the missing item and exits 1. Only ELF64 little-endian binaries are supported:
that is the only shape ``aarch64-unknown-linux-musl`` produces.

Usage::

    python scripts/check_auditable.py target/aarch64-unknown-linux-musl/release/rayd
    python scripts/check_auditable.py <elf> --expected-version 0.1.0
"""

from __future__ import annotations

import argparse
import json
import struct
import sys
import zlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import tomllib

ELF_MAGIC = b"\x7fELF"
ELF_CLASS_64 = 2
ELF_DATA_LITTLE_ENDIAN = 1
ELF64_HEADER_FORMAT = "<4sBBBBB7sHHIQQQIHHHHHH"
ELF64_HEADER_SIZE = struct.calcsize(ELF64_HEADER_FORMAT)
ELF64_SECTION_HEADER_FORMAT = "<IIQQQQIIQQ"
ELF64_SECTION_HEADER_SIZE = struct.calcsize(ELF64_SECTION_HEADER_FORMAT)
SECTION_NAME = ".dep-v0"
ROOT_PACKAGE = "rayd"
REQUIRED_PACKAGES = ("tonic", "tokio", "axum", "nix")
ROOT_CARGO_TOML = Path(__file__).resolve().parents[1] / "Cargo.toml"


class AuditableError(Exception):
    """A verification failure; the message names the missing item."""


@dataclass(frozen=True)
class Elf64Header:
    section_header_offset: int
    section_header_entry_size: int
    section_header_count: int
    section_names_index: int


@dataclass(frozen=True)
class SectionHeader:
    name_offset: int
    offset: int
    size: int


@dataclass(frozen=True)
class DependencyReport:
    package_count: int
    root_name: str
    root_version: str


def parse_elf64_header(data: bytes) -> Elf64Header:
    if len(data) < ELF64_HEADER_SIZE or data[:4] != ELF_MAGIC:
        raise AuditableError("not an ELF file")
    fields = struct.unpack_from(ELF64_HEADER_FORMAT, data)
    elf_class, elf_data = fields[1], fields[2]
    if elf_class != ELF_CLASS_64:
        raise AuditableError("not an ELF64 file")
    if elf_data != ELF_DATA_LITTLE_ENDIAN:
        raise AuditableError("not a little-endian ELF file")
    return Elf64Header(
        section_header_offset=fields[12],
        section_header_entry_size=fields[17],
        section_header_count=fields[18],
        section_names_index=fields[19],
    )


def parse_section_headers(data: bytes, header: Elf64Header) -> list[SectionHeader]:
    if header.section_header_entry_size != ELF64_SECTION_HEADER_SIZE:
        raise AuditableError(
            f"unexpected section header size {header.section_header_entry_size}"
        )
    sections: list[SectionHeader] = []
    for index in range(header.section_header_count):
        position = header.section_header_offset + index * ELF64_SECTION_HEADER_SIZE
        if position + ELF64_SECTION_HEADER_SIZE > len(data):
            raise AuditableError("section header table is truncated")
        fields = struct.unpack_from(ELF64_SECTION_HEADER_FORMAT, data, position)
        sections.append(
            SectionHeader(name_offset=fields[0], offset=fields[4], size=fields[5])
        )
    return sections


def section_name(names_table: bytes, offset: int) -> str:
    end = names_table.find(b"\0", offset)
    if end < 0:
        end = len(names_table)
    return names_table[offset:end].decode("ascii", errors="replace")


def read_section(data: bytes, wanted: str) -> bytes | None:
    header = parse_elf64_header(data)
    sections = parse_section_headers(data, header)
    if header.section_names_index >= len(sections):
        raise AuditableError("section name table index is out of range")
    names = sections[header.section_names_index]
    names_table = data[names.offset : names.offset + names.size]
    for section in sections:
        if section_name(names_table, section.name_offset) == wanted:
            return data[section.offset : section.offset + section.size]
    return None


def embedded_packages(data: bytes) -> list[dict[str, Any]]:
    section = read_section(data, SECTION_NAME)
    if section is None:
        raise AuditableError(
            f"no {SECTION_NAME} section: the binary was not built with `cargo auditable`"
        )
    try:
        document = json.loads(zlib.decompress(section))
    except (zlib.error, ValueError) as exc:
        raise AuditableError(
            f"{SECTION_NAME} is not zlib-compressed JSON: {exc}"
        ) from exc
    packages = document.get("packages") if isinstance(document, dict) else None
    if not isinstance(packages, list) or not packages:
        raise AuditableError(f"{SECTION_NAME} carries no packages")
    return packages


def workspace_version(cargo_toml: Path = ROOT_CARGO_TOML) -> str:
    manifest = tomllib.loads(cargo_toml.read_text(encoding="utf-8"))
    try:
        return str(manifest["workspace"]["package"]["version"])
    except KeyError as exc:
        raise AuditableError(
            f"{cargo_toml} has no [workspace.package].version"
        ) from exc


def verify_packages(
    packages: list[dict[str, Any]],
    expected_version: str,
    root_name: str = ROOT_PACKAGE,
    required: tuple[str, ...] = REQUIRED_PACKAGES,
) -> DependencyReport:
    roots = [package for package in packages if package.get("root") is True]
    if len(roots) != 1:
        raise AuditableError(f"expected exactly one root package, found {len(roots)}")
    root = roots[0]
    if root.get("name") != root_name:
        raise AuditableError(
            f"root package is {root.get('name')!r}, expected {root_name!r}"
        )
    if root.get("version") != expected_version:
        raise AuditableError(
            f"root package {root_name} is {root.get('version')!r}, expected {expected_version!r}"
        )
    names = {str(package.get("name")) for package in packages}
    for name in required:
        if name not in names:
            raise AuditableError(
                f"required package {name!r} is missing from {SECTION_NAME}"
            )
    return DependencyReport(
        package_count=len(packages),
        root_name=str(root["name"]),
        root_version=str(root["version"]),
    )


def check_binary(path: Path, expected_version: str) -> DependencyReport:
    if not path.is_file():
        raise AuditableError(f"{path} does not exist")
    return verify_packages(embedded_packages(path.read_bytes()), expected_version)


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("binary", type=Path, help="ELF built with `cargo auditable`")
    parser.add_argument(
        "--expected-version",
        default=None,
        help="root package version (default: [workspace.package].version of Cargo.toml)",
    )
    return parser.parse_args(argv)


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    try:
        expected = args.expected_version or workspace_version()
        report = check_binary(args.binary, expected)
    except AuditableError as exc:
        print(f"check_auditable: {exc}", file=sys.stderr)
        return 1
    print(
        f"{SECTION_NAME}: {report.package_count} packages, "
        f"root {report.root_name} {report.root_version}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
